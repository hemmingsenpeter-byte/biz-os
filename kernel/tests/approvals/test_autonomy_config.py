"""Configured (policy.yaml) and owner-set autonomy levels survive approvals and demotions."""

from __future__ import annotations

from typing import Any

from biz_kernel.approvals import autonomy, staged
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry

SCHEMA = {"type": "object"}


def _setup(config: dict[str, Any]) -> tuple[ToolRegistry, PolicyEngine]:
    reg = ToolRegistry()
    reg.register(Tool("demo.send", "demo", SideEffect.EXTERNAL, SCHEMA, lambda session, **k: None))
    return reg, PolicyEngine({"autonomy": config}, reg)


def _action() -> Action:
    return Action(
        actor="r", tool="demo.send", side_effect=SideEffect.EXTERNAL, inputs={}, decision_type="dt"
    )


def test_first_approval_does_not_reset_configured_level(session):
    reg, pol = _setup({"default": 2, "dt": 3, "demo.send": 4})
    op = staged.stage(session, _action(), "owner", "why")
    staged.approve(session, op.id, "owner", 1)  # creates stats; must not pin a level
    assert autonomy.level(session, "r", "dt", 3) == 3
    assert pol._level(session, _action()) == 3


def test_demotion_starts_from_the_configured_level(session):
    assert autonomy.record_outcome(session, "r", "dt", good=False, default=3) == 2
    assert autonomy.level(session, "r", "dt", 3) == 2


def test_promotion_starts_from_the_configured_level(session):
    assert autonomy.promote(session, "r", "dt", "owner", default=2) == 3


def test_owner_set_level_survives_approvals(session):
    reg, pol = _setup({"default": 2, "dt": 3})
    autonomy.set_level(session, "r", "dt", 4, "owner")
    op = staged.stage(session, _action(), "owner", "why")
    staged.approve(session, op.id, "owner", 1)
    assert autonomy.level(session, "r", "dt", 1) == 4


def test_strictest_of_tool_and_decision_type(session):
    reg, pol = _setup({"default": 2, "demo.send": 4, "dt": 3})
    assert pol._level(session, _action()) == 3
    autonomy.record_approval(session, "r", "dt", unchanged=True)
    assert pol._level(session, _action()) == 3
