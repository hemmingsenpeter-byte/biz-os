"""Blocked or partial operator probes must not become readiness evidence."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from deploy import scenario_smoke as smoke


def test_digest_mismatch_blocks_before_inference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "blocked.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["scenario-smoke", "--model", "local:1", "--digest", "expected", "--out", str(output)],
    )

    def unexpected_attempt(*args: object, **kwargs: object) -> None:
        pytest.fail("a mismatched model must not begin inference or create workflow databases")

    monkeypatch.setattr(smoke, "execute", unexpected_attempt)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        mock.get(smoke.ENDPOINT + "/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "local:1", "digest": "different"}]}
            )
        )
        assert smoke.main() == 2
    report = json.loads(output.read_text())
    assert len(report["results"]) == 4
    assert all(result["status"] == "BLOCKED" for result in report["results"])
    assert "not established" in report["readiness"]
    assert len(report["unverified_attempts"]) == 12
    assert output.stat().st_mode & 0o777 == 0o600


def test_incomplete_attempt_keeps_provisional_ledger_and_unverified_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "incomplete.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scenario-smoke",
            "--fixture-only",
            "--case",
            "R01",
            "--repetitions",
            "2",
            "--out",
            str(output),
        ],
    )
    attempts = []

    def incomplete_attempt(*args: object) -> dict[str, Any]:
        attempts.append(args)
        return {
            "case": "R01",
            "repetition": 1,
            "status": "FAIL",
            "error": "book_meeting did not finish",
            "checks": {"workflow completed before timeout": False},
            "ledger_status": "provisional",
            "fake_external_write_invocations": {"mail.send": 0},
        }

    monkeypatch.setattr(smoke, "execute", incomplete_attempt)
    assert smoke.main() == 1
    assert len(attempts) == 1
    report = json.loads(output.read_text())
    assert report["results"][0]["ledger_status"] == "provisional"
    assert report["results"][0]["fake_external_write_invocations"] == {"mail.send": 0}
    assert report["unverified_attempts"] == [
        {"case": "R01", "repetition": 1},
        {"case": "R01", "repetition": 2},
    ]
    assert "not established" in report["readiness"]


def test_interrupt_keeps_initial_plan_and_provisional_current_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "interrupted.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["scenario-smoke", "--fixture-only", "--repetitions", "1", "--out", str(output)],
    )

    def interrupt(*args: object) -> None:
        initial = json.loads(output.read_text())
        assert initial["results"] == []
        assert len(initial["unverified_attempts"]) == 4
        raise KeyboardInterrupt

    monkeypatch.setattr(smoke, "execute", interrupt)
    assert smoke.main() == 130
    report = json.loads(output.read_text())
    assert report["results"][0]["interrupted"] is True
    assert report["results"][0]["ledger_status"] == "provisional"
    assert len(report["unverified_attempts"]) == 4
    assert "not established" in report["readiness"]


def test_all_fixture_workflows_keep_unmet_readiness_visible(tmp_path: Path) -> None:
    output = tmp_path / "fixtures.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.scenario_smoke",
            "--fixture-only",
            "--repetitions",
            "1",
            "--out",
            str(output),
        ],
        cwd=smoke.ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(output.read_text())
    results = {result["case"]: result for result in report["results"]}
    assert set(results) == {"R01", "R04", "R12", "R15"}
    assert all(result["status"] == "PASS" for result in results.values())
    assert all(all(result["checks"].values()) for result in results.values())
    assert all(
        not any(result["fake_external_write_invocations"].values()) for result in results.values()
    )
    assert results["R04"]["readiness_checks"]["warranty ambiguity escalated"] is False
    assert results["R01"]["manual_checks"]["offered_slot_accuracy"] == "unverified"
    assert report["unverified_attempts"] == []
    assert "not established" in report["readiness"]
    assert "pilot_ready" not in report


def test_unverified_model_offer_requires_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "review.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scenario-smoke",
            "--model",
            "local:1",
            "--digest",
            "expected",
            "--case",
            "R01",
            "--repetitions",
            "1",
            "--out",
            str(output),
        ],
    )
    monkeypatch.setattr(
        smoke,
        "execute",
        lambda *args: {
            "case": "R01",
            "repetition": 1,
            "status": "REVIEW_REQUIRED",
            "checks": {"offer suppressed": True},
            "manual_checks": {"offered_slot_accuracy": "unverified"},
            "ledger_status": "observed after workflow and runtime shutdown",
        },
    )
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        mock.get(smoke.ENDPOINT + "/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "local:1", "digest": "expected"}]}
            )
        )
        mock.get(smoke.ENDPOINT + "/api/version").mock(
            return_value=httpx.Response(200, json={"version": "fixture"})
        )
        assert smoke.main() == 1
    report = json.loads(output.read_text())
    assert report["results"][0]["manual_checks"] == {"offered_slot_accuracy": "unverified"}
    assert report["unverified_attempts"] == []
