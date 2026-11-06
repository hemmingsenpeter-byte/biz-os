"""Approving a staged operation runs exactly the approved inputs, once; edits clear approval."""

from __future__ import annotations

from typing import Any

import pytest
from biz_kernel.approvals import staged
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


def engine(reg: ToolRegistry) -> PolicyEngine:
    return PolicyEngine({"autonomy": {"default": 2}}, reg)


def test_staged_operation_exact(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = engine(reg)
    op = staged.run_or_stage(session, reg, pol, "role_a", "demo.send", {"to": "x"})
    assert isinstance(op, staged.StagedOperation) and sent == []
    staged.approve(session, op.id, "owner", op.version)
    # edit clears approval; stale approval and execute both fail
    staged.edit(session, op.id, {"to": "y"})
    with pytest.raises(staged.StaleVersion):
        staged.approve(session, op.id, "owner", 1)
    with pytest.raises(staged.NotApproved):
        staged.execute(session, op.id, reg, pol)
    assert sent == []
    op = staged.approve(session, op.id, "owner", 2)
    staged.execute(session, op.id, reg, pol)
    assert sent == [{"to": "y"}]  # exactly the approved inputs
    with pytest.raises(staged.AlreadyExecuted):
        staged.execute(session, op.id, reg, pol)
    assert len(sent) == 1


def test_tampered_inputs_do_not_run(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = engine(reg)
    op = staged.run_or_stage(session, reg, pol, "role_a", "demo.send", {"to": "x"})
    staged.approve(session, op.id, "owner", 1)
    op.inputs = {"to": "evil"}  # bypassing edit()
    session.flush()
    with pytest.raises(staged.NotApproved):
        staged.execute(session, op.id, reg, pol)
    assert sent == []
