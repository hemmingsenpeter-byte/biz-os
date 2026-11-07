from datetime import UTC, datetime, timedelta
from typing import Any

from biz_kernel.budgets import BudgetBook
from biz_kernel.health import check_missed, register_schedule
from biz_kernel.heartbeats import claim_due, enqueue, run_heartbeat, schedule_cadence

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def test_skips_stopped_role_and_leaves_note(session):
    book = BudgetBook({"sales": 1})
    book.charge(session, "sales", 1, "model", None, T0)
    enqueue(session, "sales", "x", T0)
    calls = []
    sweeps: list[Any] = []
    ran = run_heartbeat(session, lambda r, rs: calls.append(r), book, sweeps.append, T0)
    assert ran == [] and calls == [] and len(sweeps) == 1
    from biz_kernel.heartbeats import Wakeup
    from sqlalchemy import select

    w = session.scalar(select(Wakeup).where(Wakeup.role == "sales"))
    assert w.status == "pending" and "budget" in w.note and w.reasons == ["x"]


def test_work_resumes_after_raise_limit(session):
    book = BudgetBook({"sales": 1})
    book.charge(session, "sales", 1, "model", None, T0)
    enqueue(session, "sales", "a", T0)
    runs = []
    t1 = T0 + timedelta(minutes=5)
    assert run_heartbeat(session, lambda r, rs: runs.append(rs), book, lambda s: 0, t1) == []
    enqueue(session, "sales", "b", t1)  # coalesces into the retained wakeup
    assert run_heartbeat(session, lambda r, rs: runs.append(rs), book, lambda s: 0, t1) == []
    assert runs == []
    book.raise_limit("sales", 5)
    t2 = T0 + timedelta(minutes=10)
    assert run_heartbeat(session, lambda r, rs: runs.append(rs), book, lambda s: 0, t2) == ["sales"]
    assert runs == [["a", "b"]]
    assert run_heartbeat(session, lambda r, rs: runs.append(rs), book, lambda s: 0, t2) == []


def test_future_wakeup_not_claimed_and_new_pending_after_claim(session):
    enqueue(session, "a", "r", T0 + timedelta(hours=1))
    assert claim_due(session, T0) == []
    assert len(claim_due(session, T0 + timedelta(hours=1))) == 1
    enqueue(session, "a", "again", T0 + timedelta(hours=1))  # new pending row, not merged
    assert len(claim_due(session, T0 + timedelta(hours=1))) == 1


def test_cadence_and_health_reporting(session):
    roles = {"ops": timedelta(minutes=15)}
    assert schedule_cadence(session, roles, T0) == []
    t1 = T0 + timedelta(minutes=15)
    assert schedule_cadence(session, roles, t1) == ["ops"]
    assert schedule_cadence(session, roles, t1) == []  # already pending
    run_heartbeat(session, lambda *a: None, BudgetBook({}), lambda s: 0, t1)
    assert check_missed(session, t1 + timedelta(minutes=5)) == []
    # heartbeat stops waking: alert
    assert check_missed(session, t1 + timedelta(minutes=40))


def test_failed_role_run_reports_failure(session):
    enqueue(session, "ops", "x", T0)

    def boom(*a):
        raise RuntimeError("nope")

    register_schedule(session, "heartbeat:ops", timedelta(hours=1), T0)
    run_heartbeat(session, boom, BudgetBook({}), lambda s: 0, T0)
    assert [a.kind for a in check_missed(session, T0)] == ["schedule.failed"]
