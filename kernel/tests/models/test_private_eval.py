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
    assert result.exit_code == 2 and message in result.output


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
