"""A role at 100% of its monthly budget makes no further paid calls; owner warned at 80%."""

from datetime import UTC, datetime
from typing import Any

import pytest
from biz_kernel.approvals import staged
from biz_kernel.budgets import BudgetBook, BudgetExceeded, CostEvent
from biz_kernel.health import open_alerts
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import func, select

T0 = datetime(2026, 3, 10, tzinfo=UTC)


def _count(session):
    return session.scalar(select(func.count()).select_from(CostEvent))


def test_budget_stops_role(session):
    book = BudgetBook({"sales": 10})
    book.charge(session, "sales", 7, "model", "a", T0)
    assert book.status(session, "sales", T0) == "ok"
    assert open_alerts(session) == []
    book.charge(session, "sales", 1, "tool", "b", T0)  # 80%
    assert book.status(session, "sales", T0) == "warn"
    book.charge(session, "sales", 0.5, "tool", "c", T0)  # still warn: no second alert
    warns = [a for a in open_alerts(session) if a.kind == "budget.warn"]
    assert len(warns) == 1
    book.charge(session, "sales", 1.5, "model", "d", T0)  # reaches 10
    assert book.status(session, "sales", T0) == "stopped"
    assert not book.can_spend(session, "sales", T0)
    before = _count(session)
    with pytest.raises(BudgetExceeded):
        book.charge(session, "sales", 0.01, "model", "e", T0)
    assert _count(session) == before  # nothing recorded
    assert book.can_spend(session, "ops", T0)  # a role without a limit is unaffected


def test_resume_by_raising_limit_or_new_month(session):
    book = BudgetBook({"sales": 1})
    book.charge(session, "sales", 1, "model", None, T0)
    assert not book.can_spend(session, "sales", T0)
    book.raise_limit("sales", 5)
    assert book.can_spend(session, "sales", T0)
    book.raise_limit("sales", 1)
    assert book.status(session, "sales", datetime(2026, 4, 1, tzinfo=UTC)) == "ok"
    assert book.spent(session, "sales", datetime(2026, 4, 1, tzinfo=UTC)) == 0


def _paid(book, side=SideEffect.READ, cost=3.0):
    calls: list[dict] = []
    reg = ToolRegistry(budgets=book, now=lambda: T0)
    reg.register(
        Tool(
            "demo.paid",
            "demo",
            side,
            {"type": "object"},
            lambda session, **k: calls.append(k),
            cost_usd=cost,
        )
    )
    reg.register(Tool("demo.free", "demo", SideEffect.READ, {"type": "object"}, lambda **k: 1))
    return reg, calls, PolicyEngine({}, reg)


def test_budget_stops_role_paid_tool_calls(session):
    book = BudgetBook({"sales": 10})
    reg, calls, pol = _paid(book)
    for _ in range(3):  # 9 of 10, past 80%
        reg.call(session, pol, actor="sales", name="demo.paid", args={})
    assert book.status(session, "sales", T0) == "warn"
    assert [a.kind for a in open_alerts(session)].count("budget.warn") == 1
    n = len(calls)
    with pytest.raises(BudgetExceeded):  # 9 + 3 would overshoot 10: refused before it runs
        reg.call(session, pol, actor="sales", name="demo.paid", args={})
    assert len(calls) == n  # the tool did not run
    assert book.spent(session, "sales", T0) == 9  # spend stops at or before the limit
    assert reg.call(session, pol, actor="sales", name="demo.free", args={}).ran  # free unaffected
    assert reg.call(session, pol, actor="ops", name="demo.paid", args={}).ran  # unmetered role


def test_callable_cost_and_failed_call_not_charged(session):
    book = BudgetBook({"sales": 10})
    reg = ToolRegistry(budgets=book, now=lambda: T0)

    def boom(session, **k):
        raise RuntimeError("provider down")

    reg.register(
        Tool(
            "demo.v",
            "demo",
            SideEffect.READ,
            {"type": "object"},
            lambda **k: 1,
            cost_usd=lambda a: a["n"] * 2.0,
        )
    )
    reg.register(Tool("demo.boom", "demo", SideEffect.READ, {"type": "object"}, boom, cost_usd=5))
    pol = PolicyEngine({}, reg)
    reg.call(session, pol, actor="sales", name="demo.v", args={"n": 2})
    assert book.spent(session, "sales", T0) == 4
    with pytest.raises(RuntimeError):
        reg.call(session, pol, actor="sales", name="demo.boom", args={})
    assert book.spent(session, "sales", T0) == 4


def test_approval_does_not_bypass_budget(session):
    book = BudgetBook({"sales": 10})
    reg, calls, pol = _paid(book, side=SideEffect.EXTERNAL)
    op = staged.run_or_stage(session, reg, pol, "sales", "demo.paid", {"x": 1}, "dt")
    staged.approve(session, op.id, "owner", 1)
    assert book.spent(session, "sales", T0) == 0  # staging costs nothing
    book.charge(session, "sales", 10, "model", None, T0)  # role exhausted meanwhile
    with pytest.raises(BudgetExceeded):
        staged.execute(session, op.id, reg, pol)
    assert calls == [] and op.status == "approved"
    book.raise_limit("sales", 50)
    staged.execute(session, op.id, reg, pol)
    assert len(calls) == 1 and book.spent(session, "sales", T0) == 13


# -- Kernel review #3: spend stops at or before the limit.


def test_charge_never_overshoots_the_limit(session):
    book = BudgetBook({"sales": 10})
    book.charge(session, "sales", 9, "tool", "a", T0)
    before = _count(session)
    with pytest.raises(BudgetExceeded):  # 9 + 2 > 10: refused, nothing recorded
        book.charge(session, "sales", 2, "tool", "b", T0)
    assert _count(session) == before and book.spent(session, "sales", T0) == 9
    book.charge(session, "sales", 1, "tool", "c", T0)  # exactly to the limit is fine
    assert book.spent(session, "sales", T0) == 10 and not book.can_spend(session, "sales", T0)


def test_charge_rejects_bad_amounts(session):
    book = BudgetBook({"sales": 10})
    for bad in (-1, "nan", "inf", float("nan"), float("-inf")):
        with pytest.raises(ValueError):
            book.charge(session, "sales", bad, "tool", None, T0)
    assert _count(session) == 0


def test_concurrent_charges_cannot_overshoot(fresh_db):
    """Two workers each see 4 of 10 spent and charge 5: only one may succeed."""
    import threading

    from biz_kernel import db
    from biz_kernel.models_registry import import_all_models
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    book = BudgetBook({"sales": 10})
    with Session(eng) as s:
        book.charge(s, "sales", 4, "tool", "seed", T0)
        s.commit()
    first, second = Session(eng), Session(eng)
    book.charge(first, "sales", 5, "tool", "first", T0)  # holds the role's lock, uncommitted
    outcome: list[object] = []

    def other() -> None:
        try:
            outcome.append(book.charge(second, "sales", 5, "tool", "second", T0))
            second.commit()
        except BudgetExceeded as exc:
            outcome.append(exc)
            second.rollback()

    t = threading.Thread(target=other)
    t.start()
    t.join(1.0)
    assert t.is_alive(), "the second charge must wait for the first to commit"
    first.commit()
    t.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], BudgetExceeded)
    with Session(eng) as s:
        assert book.spent(s, "sales", T0) == 9
    first.close()
    second.close()
    eng.dispose()


def test_budget_exceeded_survives_pickling():
    """DBOS stores step errors by pickling them, then re-raises them on replay."""
    import pickle
    from decimal import Decimal

    exc = BudgetExceeded("sales", Decimal(9), Decimal(10), Decimal(3))
    back = pickle.loads(pickle.dumps(exc))
    assert isinstance(back, BudgetExceeded) and back.role == "sales" and back.needed == 3


# -- Kernel review #6: a budget stop mid-run leaves a clear, resumable run.


def _wait(pred, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


def test_budget_stop_mid_run_resumes_when_limit_raised(fresh_db):
    import uuid
    from pathlib import Path

    from biz_kernel import db
    from biz_kernel.evidence import service as evidence
    from biz_kernel.heartbeats import service as heartbeats
    from biz_kernel.heartbeats.models import Wakeup
    from biz_kernel.models.router import Router
    from biz_kernel.models_registry import import_all_models
    from biz_kernel.packets.bus import Bus, Packet, SchemaRegistry
    from biz_kernel.records.models import Note, Task
    from biz_kernel.workflows import runner
    from biz_kernel.workflows.gate import KernelGate
    from biz_kernel.workflows.models import SopRun
    from dbos import DBOS
    from sqlalchemy import create_engine, make_url

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)

    book = BudgetBook({"r": 4})
    reg = ToolRegistry(budgets=book)

    def paid(session, **_):  # type: ignore[no-untyped-def]
        session.add(Note(author="t.paid", body="ran", source="system"))
        return {"ok": True}

    reg.register(Tool("t.paid", "t", SideEffect.READ, {"type": "object"}, paid, cost_usd=3))

    class Evidence:
        def note(self, session, kind, ref, *, task_id, job_id):  # type: ignore[no-untyped-def]
            evidence.note(session, kind, ref, job_id=job_id, task_id=task_id)

        def complete(self, session, task_id):  # type: ignore[no-untyped-def]
            evidence.complete_task(session, task_id, {"s": ["packet:demo.done"]})

    schemas = SchemaRegistry()
    schemas.register("demo.ping", 1, {"type": "object"})
    schemas.register("demo.done", 1, {"type": "object"})
    sop = {
        "name": "s",
        "trigger": "demo.ping",
        "steps": [
            {"id": "pay", "kind": "tool", "tool": "t.paid", "args": {}},
            {"id": "out", "kind": "publish", "packet": "demo.done", "payload": {}},
        ],
    }
    runner.install(
        runner.RunnerContext(
            bus=Bus(schemas),
            router=Router(local=None),  # type: ignore[arg-type]
            gate=KernelGate(reg, PolicyEngine({}, reg)),
            evidence=Evidence(),
            roles={"r": runner.RoleSpec("r", {"t.paid"}, {"s": sop}, Path("."))},
            approval_poll_seconds=30,  # long: only the heartbeat wake-up resumes the run
            budgets=book,
        )
    )
    now = db.utcnow()
    with db.session_scope() as s:
        p = Packet(type="demo.ping", schema_version=1, from_part="test", payload={})
        s.add(p)
        s.flush()
        packet_id = p.id
        book.charge(s, "r", 4, "model", "seed", now)  # spent meanwhile: the role is stopped
    DBOS(config={"name": "biz-budget", "system_database_url": sys_url, "application_version": "t"})
    DBOS.launch()
    try:
        (run_id,) = runner.start_for_packet("r", packet_id, "demo.ping")

        def run_status() -> str:
            with db.session_scope() as s:
                run = s.get(SopRun, run_id)
                return run.status if run is not None else ""

        def pending() -> list[str]:
            with db.session_scope() as s:
                w = s.scalar(select(Wakeup).where(Wakeup.role == "r", Wakeup.status == "pending"))
                return list(w.reasons) if w is not None else []

        _wait(lambda: run_status() == "budget_stopped")  # a clear state, not "running"
        kept = _wait(pending)  # the wake-up is kept
        assert any(r.startswith(f"{runner.BUDGET_RESUME}{run_id}@") for r in kept)

        def role_runner(role: str, reasons: list[str]) -> None:
            runner.resume_budget_stopped(reasons)

        def heartbeat() -> list[str]:
            with db.session_scope() as s:
                return heartbeats.run_heartbeat(s, role_runner, book, lambda s: None, db.utcnow())

        assert heartbeat() == []  # still stopped: the wake-up stays pending
        assert run_status() == "budget_stopped" and pending()
        with db.session_scope() as s:
            run = s.get(SopRun, run_id)
            assert s.get(Task, run.task_id).status == "open"
            assert s.scalar(select(func.count()).select_from(Note)) == 0  # tool never ran

        book.raise_limit("r", 6)  # under the limit again, but 4 spent + 3 needed is over 6
        assert heartbeat() == [] and pending()  # no wake-and-stop on every heartbeat
        book.raise_limit("r", 10)
        assert heartbeat() == ["r"]
        result: Any = DBOS.retrieve_workflow(run_id).get_result()
        assert result["status"] == "done"
        with db.session_scope() as s:
            run = s.get(SopRun, run_id)
            assert run.status == "done" and s.get(Task, run.task_id).status == "done"
            assert s.scalar(select(func.count()).select_from(Note)) == 1  # ran exactly once
            assert book.spent(s, "r", db.utcnow()) == 7
        assert pending() == []
        _ = uuid
    finally:
        DBOS.destroy(destroy_registry=False)
        eng.dispose()
