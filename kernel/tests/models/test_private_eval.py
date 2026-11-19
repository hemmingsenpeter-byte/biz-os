"""Offline regressions for owner review, split integrity, and private reports."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from biz_kernel.cli.main import app
from biz_kernel.models.eval import load_cases, run_eval
from biz_kernel.models.eval.baseline import FIELDS, baseline
from biz_kernel.models.eval.private import (
    REVIEW_FIELDS,
    SOURCE_FIELDS,
    TASK,
    confirm,
    csv_rows,
    csv_text,
    prepare,
    write_private,
)
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from click import unstyle
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def source_rows(count: int = 100) -> list[dict[str, str]]:
    return [
        {
            "thread_key": f"opaque-{i}",
            "channel": "mail",
            "subject": f"Case {i}",
            "body": f"=fixture note {i} from fixture@example.invalid, +64 21 555 0100"
            " see https://example.invalid/private",
        }
        for i in range(count)
    ]


@pytest.fixture
def reviewed(tmp_path: Path) -> tuple[Path, Path]:
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    write_private(source, csv_text(SOURCE_FIELDS, source_rows()))
    prepare(source, workspace, "v1")
    for split in ("tuning", "heldout"):
        path = workspace / "v1" / split / "review.csv"
        rows = csv_rows(path, REVIEW_FIELDS)
        for row in rows:
            row["category"] = "enquiry"
        write_private(path, csv_text(REVIEW_FIELDS, rows))
    assert confirm(workspace, "v1", "owner-01", ROOT) == {"tuning": 50, "heldout": 50}
    version = workspace / "v1" / "confirmed"
    return version / "tuning", version / "heldout"


def test_prepare_redacts_deduplicates_and_preserves_existing_split(tmp_path: Path) -> None:
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    rows = source_rows()
    rows.append({**rows[0], "thread_key": "alias", "subject": "Re: Case 0"})
    write_private(source, csv_text(SOURCE_FIELDS, rows))
    assert prepare(source, workspace, "v1") == {"imported": 100, "heldout": 50, "duplicates": 1}
    for split in ("tuning", "heldout"):
        path = workspace / "v1" / split / "review.csv"
        content = path.read_text()
        assert "[email]" in content and "[phone]" in content and "[link]" in content
        assert "[text] =fixture" in content
        assert (
            "fixture@example.invalid" not in content and "https://example.invalid/" not in content
        )
        assert path.stat().st_mode & 0o777 == 0o600
    write_private(source, csv_text(SOURCE_FIELDS, list(reversed(rows)) + source_rows(101)[100:]))
    prepare(source, workspace, "v2")
    for split in ("tuning", "heldout"):
        before, after = (workspace / v / split / "review.csv" for v in ("v1", "v2"))
        assert {row["case_id"] for row in csv_rows(before, REVIEW_FIELDS)} <= {
            row["case_id"] for row in csv_rows(after, REVIEW_FIELDS)
        }


def test_confirm_requires_every_label_and_rejects_changed_measurements(tmp_path: Path) -> None:
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    write_private(source, csv_text(SOURCE_FIELDS, source_rows()))
    prepare(source, workspace, "v1")
    with pytest.raises(ValueError, match="every case"):
        confirm(workspace, "v1", "owner-01", ROOT)
    assert not (workspace / "v1" / "confirmed").exists()
    sheet = workspace / "v1" / "tuning" / "review.csv"
    rows = csv_rows(sheet, REVIEW_FIELDS)
    rows[0]["body"] = "changed"
    write_private(sheet, csv_text(REVIEW_FIELDS, rows))
    with pytest.raises(ValueError, match="outside the category"):
        confirm(workspace, "v1", "owner-01", ROOT)


def test_historical_receipt_is_bound_to_the_scored_cases(reviewed: tuple[Path, Path]) -> None:
    tuning, heldout = reviewed
    assert {case.name for case in load_cases(tuning)}.isdisjoint(
        case.name for case in load_cases(heldout)
    )
    model = FakeModel()
    case_file = next(path for path in heldout.glob("*.yaml") if not path.name.startswith("_"))
    case = yaml.safe_load(case_file.read_text())
    case["expected"]["category"] = "spam"
    write_private(case_file, yaml.safe_dump(case))
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(run_eval(TASK, "local", Router(model), heldout))
    assert not model.calls


def test_private_report_meters_fake_score_without_marking_ready(
    reviewed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biz_kernel.models.eval import cli

    _, heldout = reviewed
    cases = load_cases(heldout)
    outputs = [{"category": "enquiry", "summary": "private marker", "confidence": 0.95}] * len(
        cases
    )
    monkeypatch.setattr(cli, "build_router", lambda: Router(FakeModel(outputs)))
    report = tmp_path / "report.json"
    command = [
        "eval",
        TASK,
        "--cases",
        str(heldout),
        "--output",
        str(report),
        "--local-model-version",
        "fixture-revision",
    ]
    result = CliRunner().invoke(app, command + ["--gate"])
    assert result.exit_code == 1, result.output
    assert "private marker" not in result.output
    data = json.loads(report.read_text())
    assert data["technical_gate_passed"] and not data["deployment_ready"]
    assert data["metadata"]["owner_reviewed"]
    assert data["metadata"]["review"]["case_count"] == 50
    assert data["metadata"]["model_version"] == "fixture-revision"
    assert len(data["metadata"]["dataset_sha256"]) == 64
    assert len(data["metadata"]["prompt_schema_sha256"]) == 64
    assert data["metadata"]["recorded_model_api_cost_usd"] == "0"
    assert data["results"][0]["latency_ms"] >= 0
    assert report.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--json"], "require --output and omit --json"),
        (["--tier", "cloud"], "requires --allow-cloud-data"),
        ([], "explicit model version"),
    ],
)
def test_private_cli_rejects_missing_evidence_before_using_models(
    reviewed: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    message: str,
) -> None:
    from biz_kernel.models.eval import cli

    def must_not_build() -> Router:
        raise AssertionError("model must require user evidence first")

    monkeypatch.setattr(cli, "build_router", must_not_build)
    _, heldout = reviewed
    args = ["eval", TASK, "--cases", str(heldout), "--output", str(tmp_path / "report.json")]
    result = CliRunner().invoke(app, args + extra)
    assert result.exit_code == 2 and message in unstyle(result.output)


def test_baseline_preserves_absent_review_measurements(tmp_path: Path) -> None:
    times = tmp_path / "tasks.csv"
    rows = [
        {"sample_id": f"task-{i}", "owner_seconds": "360", "approval_seconds": ""}
        for i in range(20)
    ]
    write_private(times, csv_text(FIELDS, rows))

    def summarize() -> dict:
        return baseline(times, date(2026, 11, 9), date(2026, 11, 16), 50)

    report = summarize()
    assert report["baseline_workload_hours_per_week"] == 5
    assert report["approval_review"]["mean_seconds"] is None
    rows[0]["owner_seconds"] = "nan"
    write_private(times, csv_text(FIELDS, rows))
    with pytest.raises(ValueError, match="finite"):
        summarize()


def test_synthetic_owner_review_does_not_edit_packaged_dataset(tmp_path: Path) -> None:
    cases = ROOT / "roles/receptionist/tests/eval/triage_heldout"
    metadata = (cases / "_task.yaml").read_bytes()
    output = tmp_path / "synthetic.csv"
    result = CliRunner().invoke(app, ["eval-data", "review-sheet", str(cases), str(output)])
    assert result.exit_code == 0
    assert "57 synthetic cases" in result.output
    assert (cases / "_task.yaml").read_bytes() == metadata


@pytest.mark.parametrize(
    "settings",
    [
        {"BIZ_OLLAMA_URL": "https://models.example.invalid"},
        {"BIZ_OLLAMA_URL": "http://localhost.example.invalid:11434"},
        {"BIZ_OLLAMA_MODEL": "gpt-oss:120b-cloud"},
    ],
)
def test_private_local_cli_refuses_remote_ollama_before_building_router(
    reviewed: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, str],
) -> None:
    from biz_kernel.models.eval import cli

    def must_not_build() -> Router:
        raise AssertionError("remote data requires the approved cloud tier")

    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "build_router", must_not_build)
    _, heldout = reviewed
    result = CliRunner().invoke(
        app,
        [
            "eval",
            TASK,
            "--cases",
            str(heldout),
            "--output",
            str(tmp_path / "report.json"),
            "--local-model-version",
            "fixture-revision",
        ],
    )
    assert result.exit_code == 2 and "loopback" in result.output


@pytest.mark.parametrize(
    "cloud_backend, cloud_version, ready",
    [
        ("biz_port_fakes.FakeModel", "fixture", False),
        ("biz_port_model_claude.ClaudeModel", None, False),
        ("biz_port_model_claude.ClaudeModel", "real-revision", True),
    ],
)
def test_local_recommendation_needs_a_proven_cloud_comparator(
    cloud_backend: str, cloud_version: str | None, ready: bool
) -> None:
    from biz_kernel.models.eval.harness import CaseResult, EvalReport, ShadowReport

    local = EvalReport(
        TASK,
        "local",
        1,
        [CaseResult("one", 1, [])],
        total_cases=50,
        metadata={
            "split": "heldout",
            "outcome_checks_complete": True,
            "provenance": "historical",
            "owner_reviewed": True,
            "configured_model": "pinned-local",
            "model_version": "digest",
            "backend": "biz_port_model_ollama.OllamaModel",
        },
    )
    cloud = EvalReport(
        TASK,
        "cloud",
        0,
        [CaseResult("one", 0, ["wrong category"])],
        total_cases=50,
        metadata={
            "configured_model": "cloud-model",
            "model_version": cloud_version,
            "backend": cloud_backend,
        },
    )
    report = ShadowReport(TASK, local, cloud, 0, 0.05)
    assert report.technical_gate_passed and not cloud.technical_gate_passed
    assert report.ready_for_local is ready


def test_tuning_dataset_cannot_be_relabelled_as_heldout(reviewed: tuple[Path, Path]) -> None:
    tuning, _ = reviewed
    for filename in ("_task.yaml", "_owner_review.json"):
        path = tuning / filename
        value = yaml.safe_load(path.read_text())
        value["split"] = "heldout"
        write_private(
            path, yaml.safe_dump(value) if filename.endswith(".yaml") else json.dumps(value)
        )
    model = FakeModel()
    with pytest.raises(ValueError, match="canonical|split"):
        asyncio.run(run_eval(TASK, "local", Router(model), tuning))
    assert not model.calls


def test_private_ollama_bypasses_inherited_proxies(
    reviewed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from biz_kernel.models.eval import cli
    from biz_port_model_ollama import OllamaModel

    _, heldout = reviewed
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted-proxy.example.invalid")
    monkeypatch.setenv("BIZ_OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setattr(cli, "build_router", lambda: Router(OllamaModel.create()))
    original_client = httpx.AsyncClient
    requests: list[str] = []

    def fake_ollama(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {"category": "enquiry", "summary": "fixture", "confidence": 0.9}
                    )
                }
            },
        )

    def direct_client(**options) -> httpx.AsyncClient:
        assert options["trust_env"] is False
        return original_client(transport=httpx.MockTransport(fake_ollama), **options)

    monkeypatch.setattr(cli.httpx, "AsyncClient", direct_client)
    result = CliRunner().invoke(
        app,
        [
            "eval",
            TASK,
            "--cases",
            str(heldout),
            "--output",
            str(tmp_path / "report.json"),
            "--local-model-version",
            "fixture",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert requests == ["http://127.0.0.1:11434/api/chat"] * 50


def test_tuning_dataset_cannot_replace_the_canonical_heldout_directory(
    reviewed: tuple[Path, Path],
) -> None:
    import shutil

    tuning, heldout = reviewed
    shutil.rmtree(heldout)
    shutil.copytree(tuning, heldout)
    for filename in ("_task.yaml", "_owner_review.json"):
        path = heldout / filename
        value = yaml.safe_load(path.read_text())
        value["split"] = "heldout"
        write_private(
            path, yaml.safe_dump(value) if filename.endswith(".yaml") else json.dumps(value)
        )
    model = FakeModel()
    with pytest.raises(ValueError, match="reserved cohort and split"):
        asyncio.run(run_eval(TASK, "local", Router(model), heldout))
    assert not model.calls
