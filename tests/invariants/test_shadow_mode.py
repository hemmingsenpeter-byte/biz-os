"""Shadow mode: roles read and draft, nothing outside is ever done, and every action a role
would have taken is recorded for the owner to compare (docs/PLAN.md, Stage 1)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from biz_kernel import db
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth import tokens
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from biz_kernel.loader import load_composition
from biz_kernel.models_registry import import_all_models
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.mode import SHADOW, ShadowMode, mode_of, mode_problems
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.records.models import Task
from biz_kernel.tools.registry import Tool, ToolRegistry
from biz_kernel.workflows.models import SopRun
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, make_url, select

ROOT = Path(__file__).parents[2]
KEYED = {"type": "object", "properties": {"idempotency_key": {"type": "string"}}}


def _registry(shadow: bool) -> tuple[ToolRegistry, list[str], PolicyEngine]:
    calls: list[str] = []
    reg = ToolRegistry(shadow=shadow)

    def tool(name: str, side: SideEffect, inner: tuple[str, ...] = ()) -> None:
        reg.register(
            Tool(name, "demo", side, KEYED, lambda session, **k: calls.append(name), inner=inner)
        )

    tool("demo.read", SideEffect.READ)
    tool("demo.draft", SideEffect.DRAFT)
    tool("demo.send", SideEffect.EXTERNAL)
    tool("demo.pay", SideEffect.MONEY)
    tool("demo.wrapped", SideEffect.DRAFT, inner=("demo.send",))
    tool("demo.tidy", SideEffect.DRAFT, inner=("demo.draft",))
    return reg, calls, PolicyEngine({}, reg)


def test_no_outside_effect_runs_in_shadow_not_even_approved(session) -> None:
    reg, calls, pol = _registry(shadow=True)
    for name in ("demo.send", "demo.pay", "demo.wrapped"):
        with pytest.raises(ShadowMode):
            reg.call(session, pol, actor="r", name=name, args={}, pre_approved=True)
    action = Action(actor="r", tool="demo.send", side_effect=SideEffect.EXTERNAL, inputs={})
    op = staged.stage(session, action, "owner")
    staged.approve(session, op.id, "owner", 1)
    with pytest.raises(ShadowMode):
        staged.execute(session, op.id, reg, pol)
    assert calls == [] and session.get(StagedOperation, op.id).status == "approved"
    for name in ("demo.read", "demo.draft", "demo.tidy"):  # nothing outside: runs as usual
        reg.call(session, pol, actor="r", name=name, args={})
    assert calls == ["demo.read", "demo.draft", "demo.tidy"]


def test_live_records_which_calls_acted_outside(session) -> None:
    reg, calls, pol = _registry(shadow=False)
    reg.call(session, pol, actor="r", name="demo.send", args={}, pre_approved=True)
    reg.call(session, pol, actor="r", name="demo.read", args={})
    rows = session.scalars(select(AuditEntry).where(AuditEntry.kind == "tool_call")).all()
    assert {(e.subject, e.detail["outside"]) for e in rows} == {
        ("demo.send", True),
        ("demo.read", False),
    }


def test_mode_is_checked_and_fails_safe() -> None:
    assert mode_of({}) == "live" and mode_of({"mode": "shadow"}) == SHADOW
    assert mode_of({"mode": "Live!"}) == SHADOW  # never treat a typo as live
    assert mode_problems({"mode": "Live!"}) and not mode_problems({"mode": "shadow"})


def test_hive_starts_in_shadow() -> None:
    assert mode_of(load_composition(ROOT / "apps" / "hive", ROOT).policy) == SHADOW


# ---------------------------------------------------------------- with real workflows


def _wait(pred, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


@pytest.fixture
def live(fresh_db: str, monkeypatch):  # type: ignore[no-untyped-def]
    import biz_cap_demo_desk  # noqa: F401  (registers capability tables)
    from biz_kernel.runtime import build_runtime
    from biz_kernel.workflows import runner
    from biz_port_fakes import FakeMail, FakeModel
    from dbos import DBOS

    monkeypatch.setattr(runner, "_CLIENT", None, raising=False)
    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)
    mail, model = FakeMail(), FakeModel()
    model.script({"subject": "Re: Hours", "body": "We open at nine.", "confidence": 0.9})
    rt = build_runtime(
        ROOT / "apps" / "demo",
        ROOT,
        ports={"mail": mail, "model_local": model},
        approval_poll_seconds=1,
        mode=SHADOW,
    )
    DBOS(config={"name": "biz-shadow", "system_database_url": sys_url, "application_version": "t"})
    DBOS.launch()
    yield rt, mail, model
    DBOS.destroy(destroy_registry=False)
    eng.dispose()


def _request(rt: Any) -> uuid.UUID:
    from biz_cap_demo_desk.models import DemoRequest

    with db.session_scope() as s:
        req = DemoRequest(sender="asker@example.com", subject="Hours", body="When do you open?")
        s.add(req)
        s.flush()
        packet = rt.bus.publish(
            s,
            type="service_request.received",
            from_part="test",
            summary="Hours",
            payload={
                "request_id": str(req.id),
                "channel": "desk",
                "subject": req.subject,
                "body": req.body,
            },
        )
        return uuid.UUID(str(packet.id))


def test_shadow_mode_end_to_end(live, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The role drafts the reply, nothing is staged or sent, the proposal is recorded with
    its exact inputs, the task stays open for the owner, and the report shows 0 actions."""
    from dbos import DBOS

    rt, mail, model = live
    packet_id = _request(rt)
    assert "demo_clerk" in rt.tick()

    def ended() -> SopRun | None:
        with db.session_scope() as s:
            run = s.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
            if run is not None and run.status == "shadowed":
                s.expunge(run)
                return run
            return None

    run = _wait(ended)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "shadowed"
    assert len(model.calls) == 1  # it really drafted
    assert mail.sent == []
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(StagedOperation)) == 0
        assert s.get(Task, run.task_id).status == "open"
        (proposal,) = s.scalars(select(AuditEntry).where(AuditEntry.kind == "shadow")).all()
        assert proposal.subject == "demo_desk.send_reply" and proposal.actor == "demo_clerk"
        assert proposal.detail["inputs"]["body"] == "We open at nine."
        assert proposal.detail["run_id"] == run.id
        assert proposal.detail["task_id"] == str(run.task_id)
        owner = create_user(s, "shadow-owner@example.com", "owner", "O")
        owner_id = owner.id

    monkeypatch.setenv("BIZ_SECRET_KEY", "test-secret-not-real")
    app = create_app(rt.manifest)
    with TestClient(app) as c:
        c.cookies.set("biz_session", tokens.issue_session(owner_id))
        assert c.get("/v1/control").json()["mode"] == "live"  # the manifest's own mode
        report = c.get("/v1/shadow?days=1").json()
        assert report["external_actions"] == 0
        (p,) = report["proposals"]
        assert p["tool"] == "demo_desk.send_reply" and p["run_id"] == run.id
        assert p["inputs"]["body"] == "We open at nine."
        assert c.get("/v1/shadow?days=0").status_code == 422
        c.cookies.clear()
        assert c.get("/v1/shadow").status_code == 401

    rt.tick()  # a later heartbeat neither resends nor reruns
    assert mail.sent == [] and len(model.calls) == 1


def test_shadow_route_reports_the_composition_mode(session, monkeypatch) -> None:
    monkeypatch.setenv("BIZ_SECRET_KEY", "test-secret-not-real")
    manifest = load_composition(ROOT / "apps" / "hive", ROOT)
    app = create_app(manifest)
    app.dependency_overrides[get_session] = lambda: session
    owner = create_user(session, "mode-owner@example.com", "owner", "O")
    with TestClient(app) as c:
        c.cookies.set("biz_session", tokens.issue_session(owner.id))
        assert c.get("/v1/control").json()["mode"] == "shadow"
        assert c.get("/v1/shadow").json()["mode"] == "shadow"
