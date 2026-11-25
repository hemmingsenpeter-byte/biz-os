"""Offline regressions for owner review, split integrity, and private reports."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from biz_kernel.cli.main import app
from biz_kernel.models.eval import load_cases, run_eval
from biz_kernel.models.eval.baseline import FIELDS, baseline
from biz_kernel.models.eval.private import (
    REVIEW_FIELDS,
    SOURCE_FIELDS,
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
TEMPLATE = ROOT / "roles/receptionist/tests/eval/triage_heldout"
TASK = yaml.safe_load((TEMPLATE / "_task.yaml").read_text())["task"]


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
    assert confirm(workspace, "v1", "owner-01", TEMPLATE) == {"tuning": 50, "heldout": 50}
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
        confirm(workspace, "v1", "owner-01", TEMPLATE)
    assert not (workspace / "v1" / "confirmed").exists()
    sheet = workspace / "v1" / "tuning" / "review.csv"
    rows = csv_rows(sheet, REVIEW_FIELDS)
    rows[0]["body"] = "changed"
    write_private(sheet, csv_text(REVIEW_FIELDS, rows))
    with pytest.raises(ValueError, match="outside the category"):
        confirm(workspace, "v1", "owner-01", TEMPLATE)


def test_confirm_takes_task_labels_and_checks_from_the_role_template(
    reviewed: tuple[Path, Path],
) -> None:
    _, heldout = reviewed
    task = yaml.safe_load((heldout / "_task.yaml").read_text())
    assert task["task"] == TASK and task["decision_type"] == "enquiry_classification"
    assert (heldout / "_schema.yaml").read_text() == (TEMPLATE / "_schema.yaml").read_text()
    case = load_cases(heldout)[0]
    assert {"type": "numeric_min", "field": "confidence", "min": 0.6} in case.checks
    assert json.loads(case.untrusted[0])["enquiry"]["from"] == "[redacted]"


def test_confirm_rejects_a_category_outside_the_role_schema(tmp_path: Path) -> None:
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    write_private(source, csv_text(SOURCE_FIELDS, source_rows()))
    prepare(source, workspace, "v1")
    for split in ("tuning", "heldout"):
        path = workspace / "v1" / split / "review.csv"
        rows = csv_rows(path, REVIEW_FIELDS)
        for row in rows:
            row["category"] = "not_in_schema"
        write_private(path, csv_text(REVIEW_FIELDS, rows))
    with pytest.raises(ValueError, match="valid category for every case"):
        confirm(workspace, "v1", "owner-01", TEMPLATE)
    with pytest.raises(ValueError, match="historical block"):
        confirm(workspace, "v1", "owner-01", ROOT / "roles/receptionist/tests/eval/triage")


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
            "prompt": {"matches_deployed": True},
            "scoring_contract": {"matches_deployed": True},
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


# ---- private cloud data: fail closed unless flag, app policy and operator all agree ------

OUTPUT = {"category": "enquiry", "summary": "private marker", "confidence": 0.95}


def opted_in_app(tmp_path: Path, value: object = "allow") -> Path:
    import shutil

    app_dir = tmp_path / "optin-app"
    shutil.copytree(ROOT / "apps/demo", app_dir)
    policy_file = app_dir / "policy.yaml"
    policy = yaml.safe_load(policy_file.read_text())
    policy["eval"] = {"cloud_data": value}
    policy_file.write_text(yaml.safe_dump(policy))
    return app_dir


def cloud_args(heldout: Path, report: Path, *extra: str) -> list[str]:
    return [
        "eval",
        TASK,
        "--tier",
        "cloud",
        "--cases",
        str(heldout),
        "--output",
        str(report),
        "--cloud-model-version",
        "fixture-revision",
        "--repo-root",
        str(ROOT),
        *extra,
    ]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--allow-cloud-data"], "app's policy"),
        (["--allow-cloud-data", "--app", str(ROOT / "apps/hive")], "does not allow"),
        (["--allow-cloud-data", "--app", str(ROOT / "apps/demo")], "does not allow"),
        (["--app", "OPTIN"], "requires --allow-cloud-data"),
        (["--allow-cloud-data", "--app", "OPTIN"], "--operator"),
        (["--allow-cloud-data", "--app", "OPTIN", "--operator", "a b"], "--operator"),
        (["--allow-cloud-data", "--app", "TRUE", "--operator", "op-1"], "does not allow"),
    ],
)
def test_private_cloud_data_fails_closed_without_every_authorisation(
    reviewed: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    message: str,
) -> None:
    from biz_kernel.models.eval import cli

    def must_not_build() -> Router:
        raise AssertionError("private data must not reach a model before authorisation")

    monkeypatch.setattr(cli, "build_router", must_not_build)
    optin, true_app = opted_in_app(tmp_path), opted_in_app(tmp_path / "t", True)
    swap = {"OPTIN": str(optin), "TRUE": str(true_app)}
    extra = [swap.get(a, a) for a in extra]
    _, heldout = reviewed
    result = CliRunner().invoke(app, cloud_args(heldout, tmp_path / "report.json", *extra))
    assert result.exit_code == 2, result.output
    assert message in " ".join(unstyle(result.output).replace("│", " ").split())
    assert not (heldout.parents[2] / "cloud_disclosures.jsonl").exists()


def test_hive_policy_has_no_cloud_data_opt_in() -> None:
    policy = yaml.safe_load((ROOT / "apps/hive/policy.yaml").read_text())
    assert (policy.get("eval") or {}).get("cloud_data") != "allow"


def test_harness_refuses_private_cloud_run_without_authorisation(
    reviewed: tuple[Path, Path],
) -> None:
    from biz_kernel.models.eval import shadow

    _, heldout = reviewed
    local, cloud = FakeModel(), FakeModel()
    with pytest.raises(ValueError, match="cloud"):
        asyncio.run(run_eval(TASK, "cloud", Router(local, cloud), heldout))
    with pytest.raises(ValueError, match="cloud"):
        asyncio.run(shadow(TASK, Router(local, cloud), heldout))
    assert not local.calls and not cloud.calls


def test_authorised_private_cloud_run_is_audited_without_content(
    reviewed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biz_kernel.models.eval import cli

    _, heldout = reviewed
    cloud = FakeModel([OUTPUT] * 100)
    monkeypatch.setattr(cli, "build_router", lambda: Router(FakeModel(), cloud))
    report = tmp_path / "report.json"
    args = cloud_args(
        heldout,
        report,
        "--allow-cloud-data",
        "--app",
        str(opted_in_app(tmp_path)),
        "--operator",
        "evaluator-01",
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(cloud.calls) == 50
    assert "[email]" in json.dumps(cloud.calls[0]["messages"])  # redaction still applies
    audit = heldout.parents[2] / "cloud_disclosures.jsonl"
    assert audit.stat().st_mode & 0o777 == 0o600
    (record,) = [json.loads(line) for line in audit.read_text().splitlines()]
    data = json.loads(report.read_text())
    assert record["operator"] == "evaluator-01" and record["app"] == "demo"
    assert record["case_count"] == 50 and record["split"] == "heldout"
    assert record["provider"] == "biz_port_fakes.FakeModel"
    assert record["dataset_sha256"] == data["metadata"]["dataset_sha256"]
    assert record["dataset_version"] == "v1" and "at" in record
    text = audit.read_text()
    assert "fixture note" not in text and "Case 1" not in text and "private marker" not in text
    assert data["private_data_sent_to_cloud"] is True
    assert data["cloud_data_disclosure"] == record
    assert "sent to the cloud" in result.output
    # a second run appends a second record
    assert CliRunner().invoke(app, args).exit_code == 0
    assert len(audit.read_text().splitlines()) == 2


def test_private_local_report_states_no_cloud_disclosure(
    reviewed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biz_kernel.models.eval import cli

    _, heldout = reviewed
    monkeypatch.setattr(cli, "build_router", lambda: Router(FakeModel([OUTPUT] * 50)))
    report = tmp_path / "report.json"
    args = ["eval", TASK, "--cases", str(heldout), "--output", str(report)]
    result = CliRunner().invoke(app, args + ["--local-model-version", "fixture"])
    assert result.exit_code == 0, result.output
    data = json.loads(report.read_text())
    assert data["private_data_sent_to_cloud"] is False
    assert data["cloud_data_disclosure"] is None


def test_doctor_reports_and_validates_the_cloud_data_setting(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["doctor", str(ROOT / "apps/hive")])
    assert "private evaluation data stays local" in result.output
    bad = opted_in_app(tmp_path, "yes")
    result = CliRunner().invoke(app, ["doctor", str(bad), "--repo-root", str(ROOT)])
    assert result.exit_code == 1 and "eval.cloud_data" in result.output
    good = opted_in_app(tmp_path / "g")
    result = CliRunner().invoke(app, ["doctor", str(good), "--repo-root", str(ROOT)])
    assert "may be sent to a cloud model" in result.output


# ---- review fixes -------------------------------------------------------------------------


def test_failed_cloud_case_still_records_its_spend() -> None:
    from decimal import Decimal

    from biz_kernel.models import usage
    from biz_kernel.models.eval.harness import _run_loaded, dataset_metadata

    class PaidInvalid:
        model = "fixture"

        async def structured(self, system: str, messages: list, schema: dict, tier: str) -> dict:
            usage.report(usage.Usage(10, 5, cost_usd=Decimal("0.01")))
            return {"category": "not-a-category"}

    cases = load_cases(TEMPLATE)[:2]  # packaged synthetic cases
    router = Router(FakeModel(), PaidInvalid())
    report = asyncio.run(
        _run_loaded(TASK, "cloud", router, cases, dataset_metadata(cases, cases), None)
    )
    assert all(r.error for r in report.results)
    assert report.results[0].usage and report.results[0].usage["attempts"] == 2
    assert report.metadata["recorded_model_api_cost_usd"] == "0.04"


def test_spend_survives_a_port_error_after_a_paid_attempt() -> None:
    from decimal import Decimal

    from biz_kernel.models import usage
    from biz_kernel.models.router import spent_before

    class PaidThenDown:
        calls = 0

        async def structured(self, system: str, messages: list, schema: dict, tier: str) -> dict:
            self.calls += 1
            if self.calls > 1:
                raise ConnectionError("provider down")
            usage.report(usage.Usage(cost_usd=Decimal("0.02")))
            return {}

    schema = {"type": "object", "required": ["x"]}
    router = Router(FakeModel(), PaidThenDown())
    with pytest.raises(ConnectionError) as failure:  # keeps its type: still retryable
        asyncio.run(router.generate("t", "s", [], schema, "cloud", escalate=False))
    spent = spent_before(failure.value)
    assert spent is not None and spent.cost_usd == Decimal("0.02")


def authorised(tmp_path: Path) -> Any:
    from biz_kernel.models.eval.private import authorise_cloud_data

    return authorise_cloud_data(
        allow_flag=True,
        app_dir=opted_in_app(tmp_path / "auth"),
        repo_root=ROOT,
        operator="evaluator-01",
    )


def test_cloud_authorisation_cannot_be_forged() -> None:
    from biz_kernel.models.eval.private import CloudAuthorisation

    with pytest.raises(TypeError):
        CloudAuthorisation("fixture-app", "evaluator-01")
    with pytest.raises(TypeError):
        CloudAuthorisation("fixture-app", "evaluator-01", object())


def test_authorised_private_shadow_audits_the_cloud_tier_only(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    from biz_kernel.models.eval import shadow

    _, heldout = reviewed
    grant = authorised(tmp_path)
    rep = asyncio.run(
        shadow(
            TASK,
            Router(FakeModel([OUTPUT] * 50), FakeModel([OUTPUT] * 50)),
            heldout,
            cloud_data=grant,
        )
    )
    audit = heldout.parents[2] / "cloud_disclosures.jsonl"
    (record,) = [json.loads(line) for line in audit.read_text().splitlines()]
    assert record["app"] == "demo" and record["case_count"] == 50
    assert record["tier"] == "cloud"
    assert rep.local.metadata["cloud_data_disclosure"] is None
    assert rep.cloud.metadata["cloud_data_disclosure"] == record
    assert rep.threshold == 0.1
    with pytest.raises(ValueError, match="owner-confirmed threshold"):
        router = Router(FakeModel(), FakeModel())
        asyncio.run(shadow(TASK, router, heldout, 0.5, cloud_data=grant))


def test_private_case_list_is_gated_and_audited_like_its_directory(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    from biz_kernel.models.eval import shadow
    from biz_kernel.models.eval.harness import _run_loaded, dataset_metadata

    tuning, heldout = reviewed
    subset = load_cases(heldout)[:3]
    cloud = FakeModel()
    with pytest.raises(ValueError):
        asyncio.run(shadow(TASK, Router(FakeModel(), cloud), subset))
    with pytest.raises(ValueError):  # the private helper cannot be fed bare metadata
        asyncio.run(_run_loaded(TASK, "cloud", Router(FakeModel(), cloud), subset, {}, None))
    with pytest.raises(ValueError, match="mix"):
        dataset_metadata(subset, subset + load_cases(tuning)[:1])
    assert dataset_metadata(subset, subset)["owner_reviewed"] is True
    assert not cloud.calls
    report = asyncio.run(
        run_eval(
            TASK,
            "cloud",
            Router(FakeModel(), FakeModel([OUTPUT] * 3)),
            subset,
            cloud_data=authorised(tmp_path),
        )
    )
    audit = heldout.parents[2] / "cloud_disclosures.jsonl"
    (record,) = [json.loads(line) for line in audit.read_text().splitlines()]
    assert record["case_count"] == 3
    assert report.metadata["cloud_data_disclosure"] == record


def test_library_local_tier_sends_private_cases_only_to_loopback_or_when_authorised(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    import httpx
    from biz_kernel.models.eval import shadow
    from biz_port_model_ollama import OllamaModel

    _, heldout = reviewed
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    async def go(url: str, **options: Any) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            local = OllamaModel(url, "llama3", client)
            if "cloud_data" in options:
                await shadow(TASK, Router(local, FakeModel([OUTPUT] * 50)), heldout, **options)
            else:
                await run_eval(TASK, "local", Router(local), heldout)

    with pytest.raises(ValueError, match="loopback"):
        asyncio.run(go("https://ollama.example.invalid"))
    assert seen == []
    asyncio.run(go("http://127.0.0.1:1"))  # loopback: attempted (and failing) locally
    assert seen and all(u.startswith("http://127.0.0.1:1/") for u in seen)
    seen.clear()
    # An authorised shadow run may use a remote "local" port, and audits that tier too.
    asyncio.run(go("https://ollama.example.invalid", cloud_data=authorised(tmp_path)))
    assert seen
    audit = heldout.parents[2] / "cloud_disclosures.jsonl"
    tiers = [json.loads(line)["tier"] for line in audit.read_text().splitlines()]
    assert tiers == ["local", "cloud"]


def test_receipt_binds_the_shadow_threshold(reviewed: tuple[Path, Path]) -> None:
    from biz_kernel.models.eval import shadow

    _, heldout = reviewed
    receipt = json.loads((heldout / "_owner_review.json").read_text())
    assert receipt["threshold"] == 0.1
    task_file = heldout / "_task.yaml"
    task = yaml.safe_load(task_file.read_text())
    assert task["threshold"] == 0.1
    task["threshold"] = 1.0
    write_private(task_file, yaml.safe_dump(task))
    local = FakeModel()
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(run_eval(TASK, "local", Router(local), heldout))
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(shadow(TASK, Router(local, FakeModel()), heldout))
    assert not local.calls


def _template_copy(tmp_path: Path) -> Path:
    import shutil

    template = tmp_path / "template"
    shutil.copytree(TEMPLATE, template)
    task = yaml.safe_load((template / "_task.yaml").read_text())
    task["historical"]["prompt_file"] = str(ROOT / "roles/receptionist/prompts/triage.md")
    (template / "_task.yaml").write_text(yaml.safe_dump(task))
    return template


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "required": ["category"], "properties": ["category"]},
        {"type": "object", "required": ["category"], "properties": {"category": ["enquiry"]}},
        {"type": "object", "required": None, "properties": {"category": {"enum": ["a"]}}},
        {"type": "object", "required": [], "properties": {"category": {"enum": ["a"]}}},
        {"type": "object", "required": "category", "properties": {"category": {"enum": ["a"]}}},
    ],
)
def test_read_template_rejects_malformed_schemas(tmp_path: Path, schema: dict) -> None:
    from biz_kernel.models.eval.private import read_template

    template = _template_copy(tmp_path)
    (template / "_schema.yaml").write_text(yaml.safe_dump(schema))
    with pytest.raises(ValueError, match="schema"):
        read_template(template)
    command = ["eval-data", "confirm", str(tmp_path / "ws"), "v1", "--owner", "o-1"]
    result = CliRunner().invoke(app, command + ["--template", str(template)])
    assert result.exit_code == 2, result.output


def test_read_template_rejects_an_invalid_threshold(tmp_path: Path) -> None:
    from biz_kernel.models.eval.private import read_template

    template = _template_copy(tmp_path)
    task = yaml.safe_load((template / "_task.yaml").read_text())
    task["threshold"] = 2
    (template / "_task.yaml").write_text(yaml.safe_dump(task))
    with pytest.raises(ValueError, match="threshold"):
        read_template(template)


def test_report_states_scored_prompt_and_whether_it_is_deployed(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    import hashlib
    import shutil

    _, heldout = reviewed
    prompt = (ROOT / "roles/receptionist/prompts/triage.md").read_text()
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    report = asyncio.run(
        run_eval(TASK, "local", Router(FakeModel([OUTPUT] * 50)), heldout, repo_root=ROOT)
    )
    assert report.metadata["prompt"] == {
        "scored_sha256": digest,
        "deployed_path": "roles/receptionist/prompts/triage.md",
        "deployed_sha256": digest,
        "matches_deployed": True,
    }
    report.metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    assert report.deployment_ready
    changed = tmp_path / "repo"
    shutil.copytree(ROOT / "roles", changed / "roles")
    (changed / "roles/receptionist/prompts/triage.md").write_text(prompt + "\nRevised.\n")
    report = asyncio.run(
        run_eval(TASK, "local", Router(FakeModel([OUTPUT] * 50)), heldout, repo_root=changed)
    )
    assert report.metadata["prompt"]["scored_sha256"] == digest
    assert report.metadata["prompt"]["matches_deployed"] is False
    assert report.metadata["scoring_contract"]["matches_deployed"] is True
    report.metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    assert report.technical_gate_passed and not report.deployment_ready
    receipt = json.loads((heldout / "_owner_review.json").read_text())
    assert receipt["prompt_sha256"] == digest


@pytest.fixture
def mixed_reviewed(tmp_path: Path) -> Path:
    """A confirmed cohort containing each owner-labelled outcome."""
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    write_private(source, csv_text(SOURCE_FIELDS, source_rows()))
    prepare(source, workspace, "v1")
    categories = ("enquiry", "service_request", "supplier", "spam", "other")
    for split in ("tuning", "heldout"):
        path = workspace / "v1" / split / "review.csv"
        rows = csv_rows(path, REVIEW_FIELDS)
        for number, row in enumerate(rows):
            row["category"] = categories[number % len(categories)]
        write_private(path, csv_text(REVIEW_FIELDS, rows))
    confirm(workspace, "v1", "owner-01", TEMPLATE)
    return workspace / "v1" / "confirmed" / "heldout"


@pytest.mark.parametrize(
    "expected,actual,confidence,passes",
    [
        ("other", "other", 0.25, True),
        ("other", "other", 0, True),
        ("enquiry", "enquiry", 0.59, False),
        ("enquiry", "enquiry", 0.60, True),
        ("service_request", "service_request", 0.59, False),
        ("supplier", "supplier", 0.59, False),
        ("spam", "spam", 0.59, False),
        ("other", "enquiry", 0.25, False),
        ("enquiry", "other", 0.25, False),
        ("other", "other", True, False),
        ("other", "other", "0.25", False),
        ("other", "other", None, False),
        ("other", "other", float("nan"), False),
        ("other", "other", float("inf"), False),
    ],
)
def test_historical_threshold_matches_the_owner_label(
    mixed_reviewed: Path, expected: str, actual: str, confidence: Any, passes: bool
) -> None:
    from biz_kernel.models.eval.harness import score_case

    case = next(c for c in load_cases(mixed_reviewed) if c.expected["category"] == expected)
    result = score_case(case, {"category": actual, "summary": "fixture", "confidence": confidence})
    assert result.passed is passes
    assert all("unless_expected" not in check for check in case.checks)
    assert not score_case(case, {"category": actual, "summary": "fixture"}).passed


@pytest.mark.parametrize(
    "condition",
    [None, {}, [], {"unknown": "other"}, {"category": "outside_enum"}],
)
def test_template_rejects_invalid_expected_label_conditions(tmp_path: Path, condition: Any) -> None:
    from biz_kernel.models.eval.private import read_template

    template = _template_copy(tmp_path)
    task = yaml.safe_load((template / "_task.yaml").read_text())
    task["historical"]["checks"][0]["unless_expected"] = condition
    (template / "_task.yaml").write_text(yaml.safe_dump(task))
    with pytest.raises(ValueError, match="unless_expected"):
        read_template(template)


def test_scoring_contract_change_requires_new_confirmation(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    import shutil

    from biz_kernel.models.eval.harness import EvalReport, dataset_metadata

    _, heldout = reviewed
    files = {path.name: path.read_bytes() for path in heldout.iterdir()}
    metadata = dataset_metadata(heldout, load_cases(heldout), repo_root=ROOT)
    assert metadata["scoring_contract"]["matches_deployed"] is True
    metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    assert EvalReport(TASK, "local", 1.0, [], total_cases=50, metadata=metadata).deployment_ready

    changed = tmp_path / "repo"
    shutil.copytree(ROOT / "roles", changed / "roles")
    template = changed / TEMPLATE.relative_to(ROOT)
    task = yaml.safe_load((template / "_task.yaml").read_text())
    del task["historical"]["checks"][0]["unless_expected"]
    (template / "_task.yaml").write_text(yaml.safe_dump(task))
    metadata = dataset_metadata(heldout, load_cases(heldout), repo_root=changed)
    assert metadata["owner_reviewed"] is True
    assert metadata["scoring_contract"]["matches_deployed"] is False
    metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    report = EvalReport(TASK, "local", 1.0, [], total_cases=50, metadata=metadata)
    assert report.technical_gate_passed and not report.deployment_ready
    assert files == {path.name: path.read_bytes() for path in heldout.iterdir()}
    with pytest.raises(ValueError, match="already has confirmed"):
        confirm(heldout.parents[2], "v1", "owner-01", template)


def test_tightened_current_gates_invalidate_frozen_readiness(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    import shutil
    from dataclasses import replace

    from biz_kernel.models.eval.harness import dataset_metadata

    _, heldout = reviewed
    wrong = {**OUTPUT, "category": "other"}
    report = asyncio.run(
        run_eval(
            TASK, "local", Router(FakeModel([OUTPUT] * 48 + [wrong] * 2)), heldout, repo_root=ROOT
        )
    )
    report.metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    assert report.pass_rate == 0.96 and report.deployment_ready
    original = {path.name: path.read_bytes() for path in heldout.iterdir()}
    changed = tmp_path / "repo"
    shutil.copytree(ROOT / "roles", changed / "roles")
    template = changed / TEMPLATE.relative_to(ROOT)
    task_file = template / "_task.yaml"
    task = yaml.safe_load(task_file.read_text())
    task.update(required_pass_rate=0.99, min_cases=100)
    task_file.write_text(yaml.safe_dump(task))
    metadata = {
        **report.metadata,
        **dataset_metadata(heldout, load_cases(heldout), repo_root=changed),
    }
    stale_report = replace(report, metadata=metadata)
    assert metadata["required_pass_rate"] == 0.95 and metadata["min_cases"] == 50
    assert metadata["prompt"]["matches_deployed"] is True
    assert metadata["scoring_contract"]["matches_deployed"] is False
    assert stale_report.technical_gate_passed and not stale_report.deployment_ready
    assert original == {path.name: path.read_bytes() for path in heldout.iterdir()}


@pytest.mark.parametrize(
    "key,value",
    [
        ("required_pass_rate", True),
        ("required_pass_rate", float("nan")),
        ("required_pass_rate", float("inf")),
        ("required_pass_rate", 0),
        ("required_pass_rate", 1.1),
        ("required_pass_rate", "0.99"),
        ("min_cases", True),
        ("min_cases", 0),
        ("min_cases", 1.5),
        ("min_cases", "100"),
    ],
)
def test_template_rejects_invalid_gate_settings(tmp_path: Path, key: str, value: Any) -> None:
    from biz_kernel.models.eval.private import read_template

    template = _template_copy(tmp_path)
    task_file = template / "_task.yaml"
    task = yaml.safe_load(task_file.read_text())
    task[key] = value
    task_file.write_text(yaml.safe_dump(task))
    with pytest.raises(ValueError, match=key):
        read_template(template)


def test_confirmation_saves_stricter_role_gates(
    reviewed: tuple[Path, Path], tmp_path: Path
) -> None:
    from biz_kernel.models.eval.harness import EvalReport, dataset_metadata

    _, previous = reviewed
    workspace = previous.parents[2]
    prepare(tmp_path / "source.csv", workspace, "v2")
    for split in ("tuning", "heldout"):
        path = workspace / "v2" / split / "review.csv"
        rows = csv_rows(path, REVIEW_FIELDS)
        for row in rows:
            row["category"] = "enquiry"
        write_private(path, csv_text(REVIEW_FIELDS, rows))
    template = _template_copy(tmp_path)
    task_file = template / "_task.yaml"
    task = yaml.safe_load(task_file.read_text())
    task.update(required_pass_rate=0.99, min_cases=100)
    task_file.write_text(yaml.safe_dump(task))
    confirm(workspace, "v2", "owner-01", template)
    heldout = workspace / "v2" / "confirmed" / "heldout"
    metadata = dataset_metadata(heldout, load_cases(heldout))
    receipt = json.loads((heldout / "_owner_review.json").read_text())
    assert metadata["required_pass_rate"] == receipt["required_pass_rate"] == 0.99
    assert metadata["min_cases"] == receipt["min_cases"] == 100
    assert not EvalReport(
        TASK, "local", 1.0, [], total_cases=50, metadata=metadata
    ).technical_gate_passed


@pytest.mark.parametrize("key,value", [("required_pass_rate", 0.99), ("min_cases", 100)])
def test_confirmed_gate_cannot_be_changed_without_its_receipt(
    reviewed: tuple[Path, Path], key: str, value: Any
) -> None:
    from biz_kernel.models.eval.harness import dataset_metadata

    _, heldout = reviewed
    path = heldout / "_task.yaml"
    task = yaml.safe_load(path.read_text())
    task[key] = value
    path.write_text(yaml.safe_dump(task))
    with pytest.raises(ValueError, match="receipt does not match"):
        dataset_metadata(heldout, load_cases(heldout))


def test_legacy_unconditional_cases_keep_their_original_scoring(tmp_path: Path) -> None:
    from biz_kernel.models.eval.harness import EvalReport, dataset_metadata, score_case

    template = _template_copy(tmp_path)
    task = yaml.safe_load((template / "_task.yaml").read_text())
    del task["historical"]["checks"][0]["unless_expected"]
    (template / "_task.yaml").write_text(yaml.safe_dump(task))
    source, workspace = tmp_path / "source.csv", tmp_path / "review"
    write_private(source, csv_text(SOURCE_FIELDS, source_rows()))
    prepare(source, workspace, "v1")
    for split in ("tuning", "heldout"):
        path = workspace / "v1" / split / "review.csv"
        rows = csv_rows(path, REVIEW_FIELDS)
        for row in rows:
            row["category"] = "other"
        write_private(path, csv_text(REVIEW_FIELDS, rows))
    confirm(workspace, "v1", "owner-01", template)
    heldout = workspace / "v1" / "confirmed" / "heldout"
    # Emulate a receipt predating scoring-contract attestation, without changing cases.
    for name in ("_task.yaml", "_owner_review.json"):
        path = heldout / name
        data = yaml.safe_load(path.read_text())
        del data["scoring_contract_sha256"], data["template_source"]
        write_private(path, json.dumps(data) if name.endswith(".json") else yaml.safe_dump(data))
    cases = load_cases(heldout)
    output = {"category": "other", "summary": "fixture", "confidence": 0.25}
    assert not score_case(cases[0], output).passed
    metadata = dataset_metadata(heldout, cases, repo_root=ROOT)
    assert metadata["owner_reviewed"] is True
    assert metadata["scoring_contract"]["matches_deployed"] is None
    metadata.update(
        backend="biz_port_model_ollama.OllamaModel",
        configured_model="fixture-model",
        model_version="fixture-digest",
    )
    report = EvalReport(TASK, "local", 1.0, [], total_cases=50, metadata=metadata)
    assert report.technical_gate_passed and not report.deployment_ready
