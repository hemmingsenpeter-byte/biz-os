"""Unit tests: autonomy, suggestions, queue, audit."""

from __future__ import annotations

from typing import Any

import pytest
from biz_kernel.approvals import autonomy, staged, suggestions
from biz_kernel.audit.log import AuditEntry
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import select

SCHEMA = {"type": "object"}


def setup() -> tuple[ToolRegistry, list[Any], PolicyEngine]:
    sent: list[Any] = []
    reg = ToolRegistry()
    reg.register(
        Tool("demo.send", "demo", SideEffect.EXTERNAL, SCHEMA, lambda session, **k: sent.append(k))
    )
    return reg, sent, PolicyEngine({}, reg)


def test_demotion_and_promotion(session):
    assert autonomy.level(session, "r", "d", 2) == 2
    autonomy.set_level(session, "r", "d", 3, "owner")
    assert autonomy.record_outcome(session, "r", "d", good=False) == 2
    for _ in range(5):
        autonomy.record_outcome(session, "r", "d", good=False)
    assert autonomy.level(session, "r", "d") == 1
    for _ in range(20):
        autonomy.record_approval(session, "r", "d", unchanged=True)
    assert not autonomy.promotion_due(session, "r", "d", bar=21)
    assert autonomy.promotion_due(session, "r", "d")
    assert autonomy.promote(session, "r", "d", "owner") == 2
    assert not autonomy.promotion_due(session, "r", "d")


def test_streak_resets_on_edit(session):
    reg, _, pol = setup()
    op = staged.run_or_stage(session, reg, pol, "r", "demo.send", {"a": 1}, "dt")
    staged.approve(session, op.id, "owner", 1)
    staged.edit(session, op.id, {"a": 2}, version=op.version)
    staged.approve(session, op.id, "owner", 2)
    row = autonomy._row(session, "r", "dt")
    assert row.approved_unchanged_streak == 0


def test_suggestion_flow(session):
    reg, sent, pol = setup()
    s = suggestions.propose(session, "planner", "ping", "demo.send", {"to": "x"}, ["why"], {"k": 1})
    assert sent == [] and s.status == "proposed"
    op = suggestions.approve_suggestion(session, s.id, "owner", reg)
    assert op.status == "approved" and s.staged_op_id == op.id and sent == []
    staged.execute(session, op.id, reg, pol)
    assert sent == [{"to": "x"}]


def test_queue_and_reject(session):
    reg, sent, pol = setup()
    a = staged.run_or_stage(session, reg, pol, "r", "demo.send", {})
    assert a in staged.pending(session, "owner")
    assert staged.pending(session, "nobody") == []
    staged.reject(session, a.id, "owner", "no")
    assert a not in staged.pending(session)
    with pytest.raises(staged.NotApproved):
        staged.execute(session, a.id, reg, pol)


def test_transitions_audited(session):
    reg, _, pol = setup()
    op = staged.run_or_stage(session, reg, pol, "r", "demo.send", {})
    staged.approve(session, op.id, "owner", 1)
    staged.execute(session, op.id, reg, pol)
    events = [
        e.detail.get("event")
        for e in session.scalars(select(AuditEntry).where(AuditEntry.subject == str(op.id)))
    ]
    assert {"staged", "approved", "executed"} <= set(events)
