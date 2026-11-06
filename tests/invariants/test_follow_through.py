"""Unfinished multi-step work is picked up by the next heartbeat's sweep."""

from datetime import UTC, datetime

from biz_kernel.budgets import BudgetBook
from biz_kernel.heartbeats import FollowThrough, run_heartbeat

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def test_follow_through(session):
    # a fake three-step record left half done
    progress = {"rec": 1}
    ft = FollowThrough()

    def find(_s):
        return ["rec"] if progress["rec"] < 3 else []

    def resume(_s, ref):
        progress[ref] += 1

    ft.register("fake", find, resume)
    run_heartbeat(session, lambda *a: None, BudgetBook({}), ft.sweep, T0)
    assert progress["rec"] == 2  # once per heartbeat
    run_heartbeat(session, lambda *a: None, BudgetBook({}), ft.sweep, T0)
    assert progress["rec"] == 3
    run_heartbeat(session, lambda *a: None, BudgetBook({}), ft.sweep, T0)
    assert progress["rec"] == 3  # finished work is left alone


def test_failing_item_does_not_block_others(session):
    done = []
    ft = FollowThrough()

    def boom(_s, _r):
        raise RuntimeError("x")

    ft.register("bad", lambda s: [1], boom)
    ft.register("good", lambda s: [2], lambda s, r: done.append(r))
    assert ft.sweep(session) == 1
    assert done == [2]
