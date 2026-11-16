"""Model-free release-gate regressions: full cases, explicit tiers and reproducibility."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from biz_kernel.models.eval import load_cases, run_eval, shadow
from biz_kernel.models.eval.cli import app
from biz_kernel.models.eval.harness import Case, EvalReport, report_json, score_case
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "roles/receptionist/tests/eval"


def answers(cases):
    return [{**case.expected, "summary": "Synthetic oracle", "confidence": 0.95} for case in cases]


def test_five_wrong_labels_fail_the_95_percent_gate() -> None:
    cases = load_cases(EVAL / "triage_heldout")
    outputs = answers(cases)
    for n in range(5):
        outputs[n]["category"] = "spam" if outputs[n]["category"] != "spam" else "enquiry"
    result = asyncio.run(
        run_eval(
            "receptionist.triage", "local", Router(FakeModel(outputs)), EVAL / "triage_heldout"
        )
    )
    assert result.passed_cases == 52 and result.total_cases == 57
    assert result.pass_rate == 52 / 57 and result.score == result.pass_rate
    assert result.check_score > 0.95 and not result.technical_gate_passed
    assert not result.deployment_ready


@pytest.mark.parametrize(
    "directory,task",
    [
        ("triage", "receptionist.triage"),
        ("triage_heldout", "receptionist.triage"),
        ("handle_reply", "receptionist.handle_reply"),
    ],
)
def test_all_fixture_oracles_pass_without_claiming_release(directory: str, task: str) -> None:
    cases = load_cases(EVAL / directory)
    outputs = answers(cases)
    if directory == "handle_reply":
        outputs = [{**case.expected, "confidence": 0.95} for case in cases]
    result = asyncio.run(run_eval(task, "local", Router(FakeModel(outputs)), EVAL / directory))
    assert result.passed_cases == len(cases) and result.pass_rate == 1.0
    assert result.metadata["owner_reviewed"] is False and not result.deployment_ready
    assert result.metadata["actual_tier"] == "local"
    assert result.metadata["backend"].endswith("FakeModel")
    assert len(result.metadata["dataset_sha256"]) == 64
    assert len(result.metadata["prompt_schema_sha256"]) == 64


@pytest.mark.parametrize(
    "value,passed",
    [
        (0.59995, False),
        (0.6, True),
        (0.60001, True),
        (float("nan"), False),
        (float("inf"), False),
        (True, False),
    ],
)
def test_confidence_boundary_is_exact(value, passed: bool) -> None:
    case = next(c for c in load_cases(EVAL / "triage") if c.expected["category"] == "enquiry")
    output = {**case.expected, "summary": "Synthetic", "confidence": value}
    assert score_case(case, output).passed is passed


def test_requested_cloud_cannot_fall_back_to_local() -> None:
    local = FakeModel()
    with pytest.raises(ValueError, match="cloud model"):
        asyncio.run(shadow("receptionist.triage", Router(local), EVAL / "triage_heldout"))
    with pytest.raises(ValueError, match="cloud model"):
        asyncio.run(run_eval("receptionist.triage", "cloud", Router(local), EVAL / "triage"))
    assert local.calls == []


def test_empty_missing_and_wrong_task_are_refused(tmp_path: Path) -> None:
    for path in (tmp_path, tmp_path / "missing"):
        with pytest.raises(ValueError):
            asyncio.run(shadow("t", Router(FakeModel(), FakeModel()), path))
    with pytest.raises(ValueError, match="dataset is for"):
        asyncio.run(shadow("wrong-task", Router(FakeModel(), FakeModel()), EVAL / "triage_heldout"))


def test_shadow_report_preserves_dataset_and_actual_backend_metadata() -> None:
    cases = load_cases(EVAL / "triage_heldout")
    result = asyncio.run(
        shadow(
            "receptionist.triage",
            Router(FakeModel(answers(cases)), FakeModel(answers(cases))),
            EVAL / "triage_heldout",
        )
    )
    assert result.technical_gate_passed and not result.ready_for_local
    data = json.loads(report_json(result))
    assert data["local"]["passed_cases"] == data["cloud"]["total_cases"] == 57
    assert data["local"]["metadata"]["actual_tier"] == "local"
    assert data["cloud"]["metadata"]["actual_tier"] == "cloud"
    assert data["local"]["metadata"]["dataset"] == "receptionist-triage-heldout"


def test_cli_json_report_is_private_and_gate_rejects_unreviewed_cases(
    tmp_path: Path, monkeypatch
) -> None:
    from biz_kernel.models.eval import cli

    cases = load_cases(EVAL / "triage_heldout")
    monkeypatch.setattr(cli, "build_router", lambda: Router(FakeModel(answers(cases))))
    output = tmp_path / "result.json"
    output.write_text("old")
    output.chmod(0o644)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "receptionist.triage",
            "--cases",
            str(EVAL / "triage_heldout"),
            "--json",
            "--output",
            str(output),
            "--gate",
        ],
    )
    assert result.exit_code == 1
    data = json.loads(output.read_text())
    assert data["passed_cases"] == 57 and data["deployment_ready"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(result.output)["pass_rate"] == 1.0


def test_manifest_gate_values_are_validated(tmp_path: Path) -> None:
    source = EVAL / "triage_heldout"
    cases = load_cases(source)
    for bad in (True, float("nan"), -1, 2):
        (tmp_path / "_task.yaml").write_text(yaml.safe_dump({"required_pass_rate": bad}))
        # Use the real loaded cases with metadata from a tiny on-disk valid case.
        (tmp_path / "case.yaml").write_text(
            yaml.safe_dump(
                {"schema": cases[0].schema, "expected": {}, "checks": [{"type": "json_schema"}]}
            )
        )
        with pytest.raises(ValueError, match="required_pass_rate"):
            asyncio.run(run_eval("t", "local", Router(FakeModel()), tmp_path))


@pytest.mark.parametrize("explicit_schema_only", [False, True])
def test_expected_labels_cannot_pass_a_schema_only_quality_gate(
    tmp_path: Path, explicit_schema_only: bool
) -> None:
    (tmp_path / "_task.yaml").write_text(yaml.safe_dump({"split": "heldout"}))
    for n in range(50):
        case: dict[str, Any] = {
            "schema": {"type": "object"},
            "expected": {"category": "enquiry"},
        }
        if explicit_schema_only:
            case["checks"] = [{"type": "json_schema"}]
        (tmp_path / f"{n}.yaml").write_text(yaml.safe_dump(case))
    result = asyncio.run(
        run_eval("t", "local", Router(FakeModel([{"category": "spam"}] * 50)), tmp_path)
    )
    assert not result.technical_gate_passed
    if not explicit_schema_only:
        assert result.passed_cases == 0


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1, 2, True, "bad"])
def test_invalid_shadow_margin_consumes_no_model_calls(threshold) -> None:
    local, cloud = FakeModel([{}]), FakeModel([{}])
    cases = [Case("schema", "", [], {"type": "object"}, checks=[{"type": "json_schema"}])]
    with pytest.raises(ValueError, match="threshold"):
        asyncio.run(shadow("t", Router(local, cloud), cases, threshold=threshold))
    assert local.calls == cloud.calls == []


def test_nonfinite_schema_only_output_is_a_serializable_failure() -> None:
    cases = [Case("schema", "", [], {"type": "object"}, checks=[{"type": "json_schema"}])]
    result = asyncio.run(
        run_eval("t", "local", Router(FakeModel([{"confidence": float("nan")}])), cases)
    )
    assert not result.results[0].passed and result.results[0].output is None
    assert json.loads(report_json(result))["passed_cases"] == 0


@pytest.mark.parametrize("count,rate", [(49, 1.0), (50, 0.94)])
def test_manifest_cannot_lower_the_plan_gate_floor(count: int, rate: float) -> None:
    result = EvalReport(
        "t",
        "local",
        rate,
        [],
        total_cases=count,
        metadata={
            "split": "heldout",
            "outcome_checks_complete": True,
            "min_cases": 1,
            "required_pass_rate": 0.1,
        },
    )
    assert not result.technical_gate_passed


@pytest.mark.parametrize("cloud_fails", [True, False])
def test_shadow_requires_a_real_comparison_not_a_failed_cloud_service(
    cloud_fails: bool, monkeypatch
) -> None:
    from biz_kernel.models.eval import cli

    cases = load_cases(EVAL / "triage_heldout")
    wrong = answers(cases)
    for output in wrong:
        output["category"] = "spam" if output["category"] != "spam" else "enquiry"

    def router():
        cloud = FakeModel(wrong)
        if cloud_fails:
            cloud.fail_with = RuntimeError("cloud service unavailable")
        return Router(FakeModel(answers(cases)), cloud)

    monkeypatch.setattr(cli, "build_router", router)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "receptionist.triage",
            "--tier",
            "shadow",
            "--cases",
            str(EVAL / "triage_heldout"),
            "--json",
        ],
    )
    data = json.loads(result.output)
    assert data["local"]["technical_gate_passed"] and data["cloud"]["pass_rate"] == 0.0
    assert data["comparison_valid"] is (not cloud_fails)
    assert data["technical_gate_passed"] is (not cloud_fails)
    assert result.exit_code == (1 if cloud_fails else 0)
