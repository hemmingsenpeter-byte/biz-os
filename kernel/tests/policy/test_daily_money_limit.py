"""limits.max_daily: a running total per local day, so many small payments can't add up
past the limit without approval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from biz_kernel.policy import spend
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Allow, Deny, NeedsApproval, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry

SCHEMA = {"type": "object", "properties": {"amount": {}, "idempotency_key": {}}}
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)  # 16:00 in Auckland


def _setup(limits: dict[str, Any]) -> tuple[ToolRegistry, PolicyEngine, list[Any]]:
    paid: list[Any] = []
    reg = ToolRegistry(now=lambda: NOW)
    reg.register(Tool("x.pay", "x", SideEffect.MONEY, SCHEMA, lambda session, **k: paid.append(k)))
    reg.register(Tool("x.refund", "x", SideEffect.MONEY, SCHEMA, lambda session, **k: None))
    reg.register(
        Tool("x.combo", "x", SideEffect.DRAFT, SCHEMA, lambda session, **k: None, inner=("x.pay",))
    )
    pol = PolicyEngine({"autonomy": {"default": 3}, "limits": limits}, reg)
    pol.now = lambda: NOW
    return reg, pol, paid


def _pay(session, reg, pol, amount, name="x.pay"):  # type: ignore[no-untyped-def]
    return reg.call(session, pol, actor="r", name=name, args={"amount": amount})


def test_running_total_needs_approval_past_the_daily_limit(session):
    reg, pol, paid = _setup({"max_amount": {"default": 100}, "max_daily": 150})
    assert isinstance(_pay(session, reg, pol, 80).decision, Allow)
    assert isinstance(_pay(session, reg, pol, 60).decision, Allow)  # 140
    d = _pay(session, reg, pol, 20).decision  # 160 > 150
    assert isinstance(d, NeedsApproval) and "today's total" in d.reason
    assert len(paid) == 2
    assert spend.spent_today(session, NOW, "UTC") == 140


def test_deny_mode_refuses_past_the_daily_limit(session):
    reg, pol, _ = _setup({"max_amount": 100, "max_daily": 100, "over_limit": "deny"})
    assert isinstance(_pay(session, reg, pol, 70).decision, Allow)
    assert isinstance(_pay(session, reg, pol, 40).decision, Deny)


def test_keyed_daily_limit_counts_only_that_tool(session):
    reg, pol, _ = _setup({"max_amount": 100, "max_daily": {"x.pay": 50}})
    assert isinstance(_pay(session, reg, pol, 90, "x.refund").decision, Allow)
    assert isinstance(_pay(session, reg, pol, 40).decision, Allow)
    assert isinstance(_pay(session, reg, pol, 20).decision, NeedsApproval)


def test_day_rolls_over_in_the_configured_timezone(session):
    reg, pol, _ = _setup({"max_amount": 100, "max_daily": 100, "day_timezone": "Pacific/Auckland"})
    assert isinstance(_pay(session, reg, pol, 90).decision, Allow)
    assert isinstance(_pay(session, reg, pol, 20).decision, NeedsApproval)
    later = NOW + timedelta(hours=9)  # 01:00 the next day in Auckland, same UTC day
    pol.now = reg.now = lambda: later
    assert isinstance(_pay(session, reg, pol, 20).decision, Allow)


def test_composite_tool_counts_toward_the_total(session):
    reg, pol, _ = _setup({"max_amount": 100, "max_daily": 100})
    assert isinstance(_pay(session, reg, pol, 90, "x.combo").decision, Allow)
    assert isinstance(_pay(session, reg, pol, 20).decision, NeedsApproval)


def test_unusable_daily_limit_is_denied(session):
    for bad in (-1, float("nan"), float("inf"), "100", True):
        reg, pol, _ = _setup({"max_amount": 100, "max_daily": bad})
        assert isinstance(_pay(session, reg, pol, 1).decision, Deny), bad


def test_approved_payment_past_the_total_runs_once_and_counts(session):
    from biz_kernel.approvals import staged

    reg, pol, paid = _setup({"max_amount": 100, "max_daily": 100})
    _pay(session, reg, pol, 90)
    op = staged.run_or_stage(session, reg, pol, "r", "x.pay", {"amount": 30})
    assert isinstance(op, staged.StagedOperation)
    staged.approve(session, op.id, "owner", 1)
    assert staged.execute(session, op.id, reg, pol).ran
    assert len(paid) == 2 and spend.spent_today(session, NOW, "UTC") == 120


def test_doctor_reports_unusable_limits():
    from biz_kernel.policy.engine import limit_problems

    assert limit_problems({"max_amount": {"default": 500}, "max_daily": 1000}) == []
    bad = limit_problems(
        {
            "max_amount": {"x": "500"},
            "max_daily": -1,
            "day_timezone": "Mars/Base",
            "over_limit": "?",
        }
    )
    assert len(bad) == 4


def test_unknown_timezone_is_denied(session):
    reg, pol, _ = _setup({"max_amount": 100, "max_daily": 100, "day_timezone": "Mars/Base"})
    assert isinstance(_pay(session, reg, pol, 1).decision, Deny)
