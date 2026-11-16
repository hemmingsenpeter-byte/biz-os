import asyncio
from pathlib import Path

from biz_kernel.models.eval import load_cases, run_eval, shadow
from biz_kernel.models.eval.cli import app
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from typer.testing import CliRunner

CASES = Path(__file__).resolve().parents[3] / "roles/demo_clerk/tests/eval/handle_request"


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
    perfect = asyncio.run(shadow("handle_request", r, CASES))
    assert perfect.local.pass_rate == 1.0
    assert not perfect.ready_for_local  # two tuning cases cannot authorize deployment


def test_cli_help():
    import re

    res = CliRunner().invoke(app, ["run", "--help"])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", res.output)  # CI terminals get colour codes
    assert res.exit_code == 0 and "--tier" in plain
