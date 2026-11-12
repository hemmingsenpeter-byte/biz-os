"""Pause acknowledgement is a transaction boundary for new external dispatches."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue
from threading import Event
from unittest.mock import patch

import pytest
from biz_kernel import control, db, migrations
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import AutonomyLevel, StagedOperation
from biz_kernel.budgets.models import CostEvent
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.control import service
from biz_kernel.control.models import PauseEvent
from biz_kernel.policy.types import Action, Allow, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session


class Policy:
    def check(self, session, action):
        return Allow()


def _wait_for_lock(engine, pid: int, event: str = "advisory") -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.scalar(
                text("SELECT wait_event FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
            )
        if waiting == event:
            return
        time.sleep(0.02)
    pytest.fail("operation did not wait on its advisory lock")


def test_same_millisecond_transitions_and_clock_rollback_are_ordered(session, monkeypatch) -> None:
    with (
        patch.object(db.time, "time_ns", return_value=1_000_000_000),
        patch.object(
            db.secrets, "token_bytes", side_effect=[b"\xff" * 10, b"\x00" * 10, b"\x88" * 10]
        ),
    ):
        identifiers = [db.uuid7() for _ in range(3)]
    values = iter(identifiers)

    def fixed_identifier(mapper, connection, target):
        target.id = next(values)

    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    # A mapper listener assigns the IDs even if an earlier test compiled/cached
    # SQLAlchemy's ordinary default callable.
    event.listen(PauseEvent, "before_insert", fixed_identifier)
    try:
        assert control.pause(session, "owner").paused
        assert not control.resume(session, "owner").paused
        assert control.pause(session, "staff").paused
    finally:
        event.remove(PauseEvent, "before_insert", fixed_identifier)
    rows = list(session.scalars(select(PauseEvent).order_by(PauseEvent.at)))
    assert rows[0].at < rows[1].at < rows[2].at
    assert rows[1].id < rows[0].id  # actual UUID order contradicts transition order


def test_budget_waiter_cannot_dispatch_after_pause_commits(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    budget = BudgetBook({"r": 10})
    locked, release = Event(), Event()
    sender_pid: Queue[int] = Queue()
    calls = []
    registry = ToolRegistry(budgets=budget)
    registry.register(
        Tool(
            "x.send",
            "x",
            SideEffect.EXTERNAL,
            {"type": "object"},
            lambda session: calls.append("sent"),
            cost_usd=1,
        )
    )

    def holder():
        with Session(engine) as session, session.begin():
            budget._lock(session, "r")
            locked.set()
            assert release.wait(10)

    def sender():
        try:
            with Session(engine) as session, session.begin():
                sender_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
                registry.call(session, Policy(), actor="r", name="x.send", args={})
        except control.Paused:
            return "paused"
        return "sent"

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(holder)
        assert locked.wait(10)
        sent = pool.submit(sender)
        try:
            _wait_for_lock(engine, sender_pid.get(timeout=10))
            with Session(engine) as owner, owner.begin():
                assert control.pause(owner, "owner").paused
        finally:
            release.set()
        held.result(timeout=10)
        assert sent.result(timeout=10) == "paused"
    with Session(engine) as session:
        assert calls == [] and control.is_paused(session)
        assert session.scalar(select(func.count()).select_from(CostEvent)) == 0
    engine.dispose()


def test_pause_ack_waits_for_admitted_dispatch_and_blocks_queue(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    entered, release = Event(), Event()
    pause_pid: Queue[int] = Queue()
    calls = []
    registry = ToolRegistry()

    def effect(session):
        entered.set()
        assert release.wait(10)
        # Nested/composite dispatch reuses its transaction admission, even with
        # an exclusive transition waiting. It must not deadlock behind itself.
        registry.call(session, Policy(), actor="r", name="x.inner", args={})
        calls.append("outer")

    registry.register(
        Tool(
            "x.inner",
            "x",
            SideEffect.EXTERNAL,
            {"type": "object"},
            lambda session: calls.append("inner"),
        )
    )
    registry.register(
        Tool("x.outer", "x", SideEffect.EXTERNAL, {"type": "object"}, effect, inner=("x.inner",))
    )

    def send():
        with Session(engine) as session, session.begin():
            registry.call(session, Policy(), actor="r", name="x.outer", args={})

    def pause():
        with Session(engine) as session, session.begin():
            pause_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
            return control.pause(session, "owner").paused

    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(send)
        assert entered.wait(10)
        pausing = pool.submit(pause)
        try:
            _wait_for_lock(engine, pause_pid.get(timeout=10))
            assert not pausing.done()
            with pytest.raises(control.Paused), Session(engine) as queued, queued.begin():
                registry.call(queued, Policy(), actor="other", name="x.inner", args={})
        finally:
            release.set()
        sending.result(timeout=10)
        assert pausing.result(timeout=10)
    with Session(engine) as session, session.begin(), pytest.raises(control.Paused):
        registry.call(session, Policy(), actor="r", name="x.inner", args={})
    assert calls == ["inner", "outer"]
    engine.dispose()


def test_rolled_back_pause_releases_barrier_and_changes_no_state(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    with pytest.raises(ValueError), Session(engine) as session, session.begin():
        control.pause(session, "owner")
        raise ValueError("abort transition")
    with Session(engine) as session, session.begin():
        service.admit(session)
        assert not control.is_paused(session)
    engine.dispose()


def test_approval_waiting_on_a_row_cannot_commit_after_pause(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    with Session(engine) as session, session.begin():
        operation = staged.stage(
            session,
            Action(
                actor="r",
                tool="x.send",
                side_effect=SideEffect.EXTERNAL,
                inputs={},
                decision_type="send",
            ),
            "owner",
        )
        identifier = operation.id
    approval_pid: Queue[int] = Queue()

    def approve():
        try:
            with Session(engine) as session, session.begin():
                approval_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
                staged.approve(session, identifier, "owner", 1)
        except control.Paused:
            return "paused"
        return "approved"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as holder, holder.begin():
            holder.scalar(
                select(StagedOperation).where(StagedOperation.id == identifier).with_for_update()
            )
            approval = pool.submit(approve)
            _wait_for_lock(engine, approval_pid.get(timeout=10), "transactionid")
            with Session(engine) as owner, owner.begin():
                control.pause(owner, "owner")
        assert approval.result(timeout=10) == "paused"
    with Session(engine) as session:
        operation = session.get(StagedOperation, identifier)
        assert operation is not None and operation.status == "pending"
        assert operation.approved_by is None
        assert session.scalar(select(func.count()).select_from(AutonomyLevel)) == 0
    engine.dispose()
