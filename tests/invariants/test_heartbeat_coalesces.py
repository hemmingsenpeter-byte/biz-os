"""Ten triggers for one role in a minute produce one run."""

from datetime import UTC, datetime, timedelta

from biz_kernel.budgets import BudgetBook
from biz_kernel.heartbeats import FollowThrough, enqueue, run_heartbeat

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def test_heartbeat_coalesces(session):
    for i in range(10):
        enqueue(session, "sales", f"trigger-{i}", T0 + timedelta(seconds=i * 5))
    enqueue(session, "ops", "one", T0)
    runs: list[tuple[str, list[str]]] = []
    ran = run_heartbeat(
        session,
        lambda role, reasons: runs.append((role, reasons)),
        BudgetBook({}),
        FollowThrough().sweep,
        T0 + timedelta(minutes=1),
    )
    sales = [r for r in runs if r[0] == "sales"]
    assert len(sales) == 1
    assert len(sales[0][1]) == 10
    assert sorted(ran) == ["ops", "sales"]
    # nothing left to run
    assert (
        run_heartbeat(
            session,
            lambda *a: runs.append(a),
            BudgetBook({}),
            lambda s: 0,
            T0 + timedelta(minutes=2),
        )
        == []
    )
