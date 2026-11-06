"""At autonomy levels 1 and 2, nothing leaves without a named approver."""

from __future__ import annotations

from typing import Any

import pytest
from biz_kernel.approvals import autonomy, staged
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry

SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}, "amount": {"type": "number"}}}


def make_registry(sent: list[dict[str, Any]]) -> ToolRegistry:
    reg = ToolRegistry()

    def send(session: Any, **kw: Any) -> str:
        sent.append(kw)
        return "sent"

    reg.register(Tool("demo.send", "demo", SideEffect.EXTERNAL, SCHEMA, send))
    reg.register(Tool("demo.pay", "demo", SideEffect.MONEY, SCHEMA, send))
    reg.register(Tool("demo.draft", "demo", SideEffect.DRAFT, SCHEMA, send))
    reg.register(
        Tool(
            "demo.combo", "demo", SideEffect.DRAFT, SCHEMA, send, inner=("demo.draft", "demo.send")
        )
    )
    return reg


@pytest.mark.parametrize("lvl", [1, 2])
def test_no_send_without_approval(session, lvl):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = PolicyEngine({"authority": {"demo.send": "manager"}}, reg)
    autonomy.set_level(session, "role_a", "outreach", lvl, by="owner")
    op = staged.run_or_stage(session, reg, pol, "role_a", "demo.send", {"to": "x"}, "outreach")
    assert isinstance(op, staged.StagedOperation) and op.approver == "manager"
    assert sent == []
    with pytest.raises(staged.NotApproved):
        staged.execute(session, op.id, reg, pol)
    with pytest.raises(staged.NotAuthorised):
        staged.approve(session, op.id, "role_a", 1)  # self-approval
    with pytest.raises(staged.NotAuthorised):
        staged.approve(session, op.id, "stranger", 1)
    staged.approve(session, op.id, "manager", 1)
    staged.execute(session, op.id, reg, pol)
    assert len(sent) == 1


def test_level_3_acts_without_approval(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = PolicyEngine({}, reg)
    autonomy.set_level(session, "role_a", "outreach", 3, by="owner")
    r = staged.run_or_stage(session, reg, pol, "role_a", "demo.send", {"to": "x"}, "outreach")
    assert r.ran and len(sent) == 1
