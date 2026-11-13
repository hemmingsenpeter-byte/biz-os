import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from biz_kernel.cli.main import app as biz_app
from biz_kernel.models.eval import (
    Case,
    Gate,
    load_cases,
    load_task_config,
    report_text,
    run_eval,
    score_case,
    shadow,
)
from biz_kernel.models.eval import cli as eval_cli
from biz_kernel.models.eval.cli import app
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from typer.testing import CliRunner

CASES = Path(__file__).resolve().parents[3] / "roles/demo_clerk/tests/eval/handle_request"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["intent", "confidence"],
    "properties": {
        "intent": {"enum": ["yes", "no"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "note": {"type": "string"},
    },
}
CHECKS = [{"type": "json_schema"}, {"type": "exact", "fields": ["intent"]}]
GOOD = {"intent": "yes", "confidence": 0.9}
BAD = {"intent": "no", "confidence": 0.9}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)  # CI terminals get colour codes


def _cases(n: int) -> list[Case]:
    msgs = [{"role": "user", "content": "x"}]
    return [Case(f"case_{i:02d}", "sys", msgs, SCHEMA, {"intent": "yes"}, CHECKS) for i in range(n)]


def _answers(good: int, bad: int) -> list[dict[str, Any]]:
    return [GOOD] * good + [BAD] * bad


def _run(cases: Any, local: FakeModel, gate: Gate | None = None) -> Any:
    return asyncio.run(run_eval("t", "local", Router(local, FakeModel()), cases, gate))


def _write_dir(tmp: Path, n: int, task: str | None = None) -> Path:
    for i in range(n):
        (tmp / f"case_{i:02d}.yaml").write_text(
            json.dumps(
                {
                    "name": f"case_{i:02d}",
                    "prompt": "x",
                    "schema": SCHEMA,
                    "expected": {"intent": "yes"},
                    "checks": CHECKS,
                }
            )
        )
    if task is not None:
        (tmp / "_task.yaml").write_text(task)
    return tmp


# ---- existing behaviour --------------------------------------------------------------------


def test_load_and_run():
    cases = load_cases(CASES)
    assert len(cases) == 2 and "polite" in cases[0].system
    local = FakeModel(
        [{"subject": "Question", "body": "b"}, {"subject": "s", "body": "Our holiday hours"}]
    )
    rep = asyncio.run(run_eval("handle_request", "local", Router(local, FakeModel()), CASES))
    assert rep.score == 1.0
    assert "<untrusted>" in local.calls[0]["messages"][-1]["content"]


def test_shadow_threshold():
    good = [{"subject": "Question", "body": "b"}, {"subject": "s", "body": "holiday"}]
    bad = [{"subject": "Question", "body": "b"}, {"subject": "s", "body": "nothing"}]
    r = Router(FakeModel(bad), FakeModel(good))
    rep = asyncio.run(shadow("handle_request", r, CASES))
    assert rep.local.score < rep.cloud.score and not rep.ready_for_local
    assert rep.agreement == 0.5
    r = Router(FakeModel(good), FakeModel(good))
    assert asyncio.run(shadow("handle_request", r, CASES)).ready_for_local


def test_cli_help():
    res = CliRunner().invoke(app, ["run", "--help"])
    plain = _plain(res.output)
    assert res.exit_code == 0 and "--tier" in plain and "--json" in plain


# ---- report fields and the gate ------------------------------------------------------------


def test_report_counts_cases_passed_not_mean_score():
    rep = _run(_cases(50), FakeModel(_answers(48, 2)))
    assert (rep.passed, rep.total) == (48, 50)
    assert rep.pass_rate == pytest.approx(0.96)
    assert rep.score == pytest.approx(0.98)  # failing cases still pass json_schema: 0.5 each
    assert rep.gate.passed and "48 of 50" in rep.gate.reason
    assert [r.name for r in rep.failed] == ["case_48", "case_49"]
    d = rep.as_dict()
    assert d["passed"] == 48 and d["total"] == 50 and d["pass_rate"] == pytest.approx(0.96)
    assert d["gate"]["passed"] is True and d["gate"]["min_cases"] == 50
    assert d["score"] == pytest.approx(0.98) and len(d["results"]) == 50


def test_gate_fails_below_threshold():
    rep = _run(_cases(50), FakeModel(_answers(47, 3)))
    assert rep.pass_rate == pytest.approx(0.94)
    assert not rep.gate.passed and "below 95.0%" in rep.gate.reason


def test_gate_fails_with_too_few_cases_even_when_all_pass():
    rep = _run(_cases(49), FakeModel(_answers(49, 0)))
    assert rep.pass_rate == 1.0
    assert not rep.gate.passed and "only 49 cases" in rep.gate.reason


def test_gate_passes_at_exactly_the_threshold():
    rep = _run(_cases(60), FakeModel(_answers(57, 3)))
    assert rep.pass_rate == pytest.approx(0.95) and rep.gate.passed


def test_gate_with_no_cases_fails():
    rep = _run([], FakeModel())
    assert rep.total == 0 and rep.pass_rate == 0.0 and not rep.gate.passed


def test_failed_model_call_counts_as_failed_case():
    local = FakeModel()
    local.fail_with = RuntimeError("model down")
    rep = _run(_cases(2), local, Gate(0.5, 1))
    assert rep.passed == 0 and rep.failed[0].error == "model down" and not rep.gate.passed


def test_gate_and_threshold_from_task_yaml(tmp_path: Path):
    d = _write_dir(tmp_path, 4, "threshold: 0.2\ngate: { pass_rate: 0.75, min_cases: 4 }\n")
    cfg = load_task_config(d)
    assert cfg.threshold == 0.2 and cfg.gate == Gate(0.75, 4)
    rep = _run(d, FakeModel(_answers(3, 1)))
    assert rep.gate.passed and rep.gate.min_pass_rate == 0.75
    assert not _run(d, FakeModel(_answers(2, 2))).gate.passed


def test_defaults_without_gate_in_task_yaml(tmp_path: Path):
    assert load_task_config(_write_dir(tmp_path, 1, "threshold: 0.1\n")).gate == Gate(0.95, 50)
    assert load_task_config(None).gate == Gate()


def test_shadow_reports_both_tiers_pass_rates():
    r = Router(FakeModel(_answers(45, 5)), FakeModel(_answers(50, 0)))
    rep = asyncio.run(shadow("t", r, _cases(50)))
    assert rep.local.pass_rate == pytest.approx(0.9) and rep.cloud.pass_rate == 1.0
    assert not rep.local.gate.passed and rep.cloud.gate.passed
    d = rep.as_dict()
    assert d["local_pass_rate"] == pytest.approx(0.9) and d["cloud_pass_rate"] == 1.0
    assert d["local"]["gate"]["passed"] is False and d["cloud"]["gate"]["passed"] is True
    text = report_text(rep)
    assert "t [local]: 45 of 50" in text and "t [cloud]: 50 of 50" in text


# ---- check types ---------------------------------------------------------------------------


def _one(check: dict[str, Any], out: dict[str, Any], expected: dict[str, Any] | None = None):
    c = Case("c", "", [], SCHEMA, expected or {"intent": "yes"}, [check])
    return score_case(c, out)


CATEGORY_OR_UNSURE = {
    "type": "any_of",
    "checks": [
        {"type": "exact", "fields": ["intent"]},
        {"type": "max", "field": "confidence", "value": 0.6, "exclusive": True},
    ],
}


@pytest.mark.parametrize(
    ("out", "ok"),
    [
        ({"intent": "yes", "confidence": 0.9}, True),  # right intent
        ({"intent": "no", "confidence": 0.5}, True),  # wrong, but unsure
        ({"intent": "no", "confidence": 0.6}, False),  # wrong, at the ceiling
        ({"intent": "no", "confidence": 0.9}, False),  # wrong and sure
    ],
)
def test_any_of(out: dict[str, Any], ok: bool):
    res = _one(CATEGORY_OR_UNSURE, out)
    assert res.passed is ok
    if not ok:
        assert res.failures[0].startswith("any_of: none passed (exact:")
        assert "max: confidence" in res.failures[0]


def test_not_equal():
    chk = {"type": "not_equal", "field": "intent", "value": "yes"}
    assert _one(chk, {"intent": "no"}).passed
    assert _one(chk, {}).passed  # missing is not equal
    res = _one(chk, {"intent": "yes"})
    assert not res.passed and res.failures == ["not_equal: intent = 'yes'"]


def test_not_contains():
    chk = {"type": "not_contains", "field": "note", "value": "Refund"}
    assert _one(chk, {"note": "see you then"}).passed
    assert _one(chk, {}).passed
    res = _one(chk, {"note": "a full REFUND today"})
    assert not res.passed and res.failures == ["not_contains: note contains 'Refund'"]


@pytest.mark.parametrize(
    ("check", "value", "ok"),
    [
        ({"type": "min", "value": 0.7}, 0.7, True),
        ({"type": "min", "value": 0.7}, 0.69, False),
        ({"type": "min", "value": 0.7, "exclusive": True}, 0.7, False),
        ({"type": "min", "value": 0.7, "exclusive": True}, 0.71, True),
        ({"type": "max", "value": 0.6}, 0.6, True),
        ({"type": "max", "value": 0.6}, 0.61, False),
        ({"type": "max", "value": 0.6, "exclusive": True}, 0.6, False),
        ({"type": "max", "value": 0.6, "exclusive": True}, 0.59, True),
        ({"type": "min", "value": 0}, None, False),  # missing
        ({"type": "min", "value": 0}, "0.9", False),  # not a number
        ({"type": "max", "value": 1}, True, False),  # bools are not numbers
    ],
)
def test_min_max(check: dict[str, Any], value: Any, ok: bool):
    out = {} if value is None else {"confidence": value}
    res = _one({**check, "field": "confidence"}, out)
    assert res.passed is ok, res.failures
    if not ok:
        assert res.failures[0].startswith(f"{check['type']}: confidence")


@pytest.mark.parametrize(
    ("check", "message"),
    [
        ({"type": "nearly"}, "unknown check type 'nearly'"),
        ({"type": "min", "field": "confidence"}, "min check needs value"),
        ({"type": "any_of", "checks": []}, "any_of needs a non-empty list"),
        ({"type": "any_of", "checks": [{"type": "max"}]}, "max check needs field, value"),
        ({"field": "x"}, "a check must be a mapping with a type"),
    ],
)
def test_load_cases_rejects_bad_checks(tmp_path: Path, check: dict[str, Any], message: str):
    (tmp_path / "bad.yaml").write_text(json.dumps({"prompt": "x", "checks": [check]}))
    with pytest.raises(ValueError, match=re.escape(message)):
        load_cases(tmp_path)


# ---- `biz eval` ----------------------------------------------------------------------------


@pytest.fixture
def fake_router(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, Router] = {}

    def use(local: list[dict[str, Any]], cloud: list[dict[str, Any]] | None = None) -> None:
        holder["r"] = Router(FakeModel(local), FakeModel(cloud or []))

    monkeypatch.setattr(eval_cli, "build_router", lambda: holder["r"])
    return use


def _biz(*args: str):
    return CliRunner().invoke(biz_app, ["eval", "t", *args])


def test_biz_eval_exits_0_when_gate_passes(tmp_path: Path, fake_router):
    fake_router(_answers(49, 1))
    res = _biz("--cases", str(_write_dir(tmp_path, 50)))
    out = _plain(res.output)
    assert res.exit_code == 0, out
    assert "t [local]: 49 of 50 cases passed (98.0%)" in out and "gate: PASS" in out
    assert "case_49: exact: intent = 'no', expected 'yes'" in out


def test_biz_eval_exits_1_below_threshold(tmp_path: Path, fake_router):
    fake_router(_answers(45, 5))
    res = _biz("--cases", str(_write_dir(tmp_path, 50)))
    out = _plain(res.output)
    assert res.exit_code == 1, out
    assert "gate: FAIL - pass rate 90.0% is below 95.0%" in out
    assert out.count("expected 'yes'") == 5


def test_biz_eval_exits_1_with_too_few_cases(tmp_path: Path, fake_router):
    fake_router(_answers(10, 0))
    res = _biz("--cases", str(_write_dir(tmp_path, 10)))
    assert res.exit_code == 1 and "only 10 cases, the gate needs at least 50" in res.output


def test_biz_eval_one_failure_no_longer_fails_the_run(tmp_path: Path, fake_router):
    fake_router(_answers(3, 1))
    d = _write_dir(tmp_path, 4, "gate: { pass_rate: 0.75, min_cases: 4 }\n")
    assert _biz("--cases", str(d)).exit_code == 0


def test_biz_eval_json(tmp_path: Path, fake_router):
    fake_router(_answers(45, 5))
    res = _biz("--cases", str(_write_dir(tmp_path, 50)), "--json")
    assert res.exit_code == 1
    body = json.loads(res.output)
    assert body["passed"] == 45 and body["total"] == 50 and body["pass_rate"] == 0.9
    assert body["gate"]["passed"] is False and "score" in body


def test_biz_eval_shadow_follows_the_local_gate(tmp_path: Path, fake_router):
    d = _write_dir(tmp_path, 50)
    fake_router(_answers(45, 5), _answers(50, 0))
    res = _biz("--cases", str(d), "--tier", "shadow")
    assert res.exit_code == 1
    assert "t [local]: 45 of 50" in res.output and "t [cloud]: 50 of 50" in res.output
    fake_router(_answers(50, 0), _answers(45, 5))
    res = _biz("--cases", str(d), "--tier", "shadow", "--json")
    assert res.exit_code == 0
    body = json.loads(res.output)
    assert body["local_pass_rate"] == 1.0 and body["cloud_pass_rate"] == 0.9


def test_biz_eval_rejects_unknown_tier(tmp_path: Path):
    with pytest.raises(typer.BadParameter):
        eval_cli.eval_cmd("t", "medium", tmp_path, router=Router(FakeModel(), FakeModel()))
