"""While paused, no role runs and nothing with an outside effect happens, approved or not.
Work is kept, and runs exactly once after the resume."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from biz_kernel import control, db
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.budgets import BudgetBook
from biz_kernel.control.models import PauseEvent
from biz_kernel.heartbeats import service as heartbeats
from biz_kernel.heartbeats.models import Wakeup
from biz_kernel.models_registry import import_all_models
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from biz_kernel.workflows.models import SopRun
from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.exc import DBAPIError

T0 = datetime(2026, 3, 10, tzinfo=UTC)
ROOT = Path(__file__).parents[2]
KEYED = {"type": "object", "properties": {"idempotency_key": {"type": "string"}}}


def _registry() -> tuple[ToolRegistry, list[str], PolicyEngine]:
    calls: list[str] = []
    reg = ToolRegistry()

    def tool(name: str, side: SideEffect, inner: tuple[str, ...] = ()) -> None:
        reg.register(
            Tool(name, "demo", side, KEYED, lambda session, **k: calls.append(name), inner=inner)
        )

    tool("demo.read", SideEffect.READ)
    tool("demo.draft", SideEffect.DRAFT)
    tool("demo.send", SideEffect.EXTERNAL)
    tool("demo.pay", SideEffect.MONEY)
    tool("demo.wrapped", SideEffect.DRAFT, inner=("demo.send",))
    return reg, calls, PolicyEngine({}, reg)


def test_paused_blocks_every_outside_effect(session) -> None:
    reg, calls, pol = _registry()
    control.pause(session, "owner", "something looks wrong")
    for name in ("demo.send", "demo.pay", "demo.wrapped"):
        with pytest.raises(control.Paused):  # even a call that replays an approval
            reg.call(session, pol, actor="r", name=name, args={}, pre_approved=True)
    assert calls == []
    reg.call(session, pol, actor="r", name="demo.read", args={})  # reading is harmless
    reg.call(session, pol, actor="r", name="demo.draft", args={})
    assert calls == ["demo.read", "demo.draft"]


def test_approved_operation_waits_then_runs_once(session) -> None:
    reg, calls, pol = _registry()
    action = Action(actor="r", tool="demo.send", side_effect=SideEffect.EXTERNAL, inputs={})
    op = staged.stage(session, action, "owner")
    staged.approve(session, op.id, "owner", 1)
    control.pause(session, "owner")
    with pytest.raises(control.Paused):
        staged.execute(session, op.id, reg, pol)
    assert calls == [] and session.get(StagedOperation, op.id).status == "approved"
    control.resume(session, "owner")
    staged.execute(session, op.id, reg, pol)
    with pytest.raises(staged.AlreadyExecuted):
        staged.execute(session, op.id, reg, pol)
    assert calls == ["demo.send"]


def test_paused_heartbeat_runs_no_role_and_keeps_the_work(session) -> None:
    ran: list[str] = []
    swept: list[int] = []
    book = BudgetBook({})

    def beat() -> list[str]:
        return heartbeats.run_heartbeat(
            session, lambda role, reasons: ran.append(role), book, lambda s: swept.append(1), T0
        )

    heartbeats.enqueue(session, "r", "packet:x", T0)
    control.pause(session, "owner")
    assert beat() == [] and ran == [] and swept == []
    pending = session.scalar(select(Wakeup).where(Wakeup.role == "r"))
    assert pending.status == "pending" and pending.reasons == ["packet:x"]
    control.resume(session, "owner")
    assert beat() == ["r"] and ran == ["r"]
    assert beat() == [] and ran == ["r"]  # once


def test_pause_state_is_idempotent_and_recorded(session) -> None:
    assert not control.is_paused(session) and control.state(session).paused is False
    first = control.pause(session, "a@example.com", "first reason")
    again = control.pause(session, "owner", "second reason")
    assert first.paused and again.by == "a@example.com" and again.reason == "first reason"
    assert control.resume(session, "owner").paused is False
    assert control.resume(session, "owner").paused is False
    assert len(session.scalars(select(PauseEvent)).all()) == 2


def test_pause_events_are_append_only(session) -> None:
    control.pause(session, "owner")
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text("update control_pauses set paused = false"))


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

    monkeypatch.setattr(runner, "_CLIENT", None, raising=False)  # this process runs DBOS
    from biz_port_fakes import FakeMail, FakeModel
    from dbos import DBOS

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
        approval_poll_seconds=30,  # long: only the resume wakes a parked run
    )
    DBOS(config={"name": "biz-pause", "system_database_url": sys_url, "application_version": "t"})
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


def _run(packet_id: uuid.UUID, status: str) -> SopRun | None:
    with db.session_scope() as s:
        run = s.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
        return run if run is not None and run.status == status else None


def test_kill_switch(live) -> None:  # type: ignore[no-untyped-def]
    """Paused before the heartbeat: nothing starts. Paused between approval and sending:
    the approved message is not sent. After the resume it is sent, once."""
    from biz_kernel.workflows.gate import decide
    from biz_kernel.workflows.runner import notify_decision
    from dbos import DBOS

    rt, mail, model = live
    packet_id = _request(rt)
    with db.session_scope() as s:
        control.pause(s, "owner", "testing")
    assert rt.tick() == []  # no role wakes
    assert model.calls == [] and _run(packet_id, "running") is None
    with db.session_scope() as s:
        control.resume(s, "owner")

    assert "demo_clerk" in rt.tick()
    run = _wait(lambda: _run(packet_id, "waiting"))
    op_id = uuid.UUID(run.waiting_op_id)

    with db.session_scope() as s:
        control.pause(s, "owner", "testing")
    with pytest.raises(control.Paused):  # nothing can be approved while paused
        decide(op_id, "demo_owner", approve=True, version=1)
    # The race: the approval landed just before the pause, the worker wakes just after.
    with db.session_scope() as s:
        staged.approve(s, op_id, "demo_owner", 1)
    notify_decision(str(op_id), "approved")
    _wait(lambda: _run(packet_id, "paused"))
    assert mail.sent == []
    assert rt.tick() == [] and mail.sent == []  # still paused: a heartbeat changes nothing

    with db.session_scope() as s:
        control.resume(s, "owner")
    rt.tick()  # the follow-through sweep wakes parked runs
    result: Any = DBOS.retrieve_workflow(run.id).get_result()
    assert result["status"] == "done"
    assert len(mail.sent) == 1
    rt.tick()
    assert len(mail.sent) == 1 and _run(packet_id, "done") is not None
