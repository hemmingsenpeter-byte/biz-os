"""Every external or money tool call passes policy.check."""

from __future__ import annotations

from typing import Any

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


class Spy:
    def __init__(self, inner: PolicyEngine) -> None:
        self.inner, self.seen = inner, []

    def check(self, session, action):
        self.seen.append(action.tool)
        return self.inner.check(session, action)


def test_no_external_without_policy(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    spy = Spy(PolicyEngine({"autonomy": {"default": 4}}, reg))
    for name in ("demo.send", "demo.pay", "demo.combo"):
        reg.call(session, spy, actor="r", name=name, args={"to": "x"})
    assert spy.seen == ["demo.send", "demo.pay", "demo.combo"]
    reg.call(session, spy, actor="r", name="demo.draft", args={})
    assert "demo.draft" not in spy.seen


def test_default_policy_gates_everything(session):
    sent: list[dict[str, Any]] = []
    reg = make_registry(sent)
    pol = PolicyEngine({}, reg)  # empty config: nothing external runs unattended
    for name in ("demo.send", "demo.pay"):
        assert not reg.call(session, pol, actor="r", name=name, args={}).ran
    assert sent == []
