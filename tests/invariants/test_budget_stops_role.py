"""A role at 100% of its monthly budget makes no further paid calls; owner warned at 80%."""

from datetime import UTC, datetime

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
    reg.call(session, pol, actor="sales", name="demo.paid", args={})  # 12 >= 10 now
    assert book.status(session, "sales", T0) == "stopped"
    n = len(calls)
    with pytest.raises(BudgetExceeded):
        reg.call(session, pol, actor="sales", name="demo.paid", args={})
    assert len(calls) == n  # the tool did not run
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
