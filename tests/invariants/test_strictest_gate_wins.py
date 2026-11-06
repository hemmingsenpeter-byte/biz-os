"""A composite action never runs an inner tool stricter than the composite approval allows."""

from __future__ import annotations

from typing import Any

from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, Allow, Deny, NeedsApproval, SideEffect, strictest
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


def test_strictest_gate_wins(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = PolicyEngine(
        {"autonomy": {"default": 4}, "hard_rules": {"deny_tools": ["demo.pay"]}}, reg
    )
    reg.register(
        Tool(
            "demo.bundle",
            "demo",
            SideEffect.DRAFT,
            SCHEMA,
            lambda session, **k: 1,
            inner=("demo.draft", "demo.pay"),
        )
    )
    # inner money tool is denied -> composite is denied even though outer is a draft
    r = reg.call(session, pol, actor="r", name="demo.bundle", args={})
    assert isinstance(r.decision, Deny) and not r.ran

    # inner external at level 2 -> composite needs approval, never silently runs
    pol2 = PolicyEngine({"autonomy": {"default": 2}}, reg)
    r = reg.call(session, pol2, actor="r", name="demo.combo", args={"to": "x"})
    assert isinstance(r.decision, NeedsApproval) and sent == []

    # unknown inner tool is treated as external
    pol3 = PolicyEngine({"autonomy": {"default": 2}})
    act = Action("r", "demo.combo", SideEffect.DRAFT, inner=("demo.draft", "demo.send"))
    assert isinstance(pol3.check(session, act), NeedsApproval)
    assert isinstance(strictest([Allow(), NeedsApproval("o"), Deny("x")]), Deny)
