"""Regressions from the M0/M1 safety review: limits and tool settings can't be loosened."""

from biz_kernel.approvals import staged
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, Allow, Deny, NeedsApproval, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry

AMOUNT = {"type": "object", "properties": {"amount": {"type": "number"}}}
LIMITS = {"max_amount": {"default": 500}, "over_limit": "deny"}


def test_deny_limit_holds_at_every_autonomy_level(session):
    paid: list[dict] = []
    reg = ToolRegistry()
    reg.register(Tool("x.pay", "x", SideEffect.MONEY, AMOUNT, lambda session, **k: paid.append(k)))
    for level in (1, 2, 3, 4):
        pol = PolicyEngine({"autonomy": {"default": level}, "limits": LIMITS}, reg)
        big = Action("r", "x.pay", SideEffect.MONEY, amount=10000)
        assert isinstance(pol.check(session, big), Deny), level
        res = staged.run_or_stage(session, reg, pol, "r", "x.pay", {"amount": 10000})
        assert not isinstance(res, staged.StagedOperation), "a denied payment must not be staged"
    assert paid == []


def test_under_limit_low_level_still_needs_approval(session):
    reg = ToolRegistry()
    reg.register(Tool("x.pay", "x", SideEffect.MONEY, AMOUNT, lambda session, **k: None))
    pol = PolicyEngine({"autonomy": {"default": 2}, "limits": LIMITS}, reg)
    small = Action("r", "x.pay", SideEffect.MONEY, amount=10)
    assert isinstance(pol.check(session, small), NeedsApproval)


def test_decision_type_cannot_loosen_a_tool_or_its_inner_tools(session):
    sent: list[dict] = []
    reg = ToolRegistry()
    send = Tool(
        "x.send", "x", SideEffect.EXTERNAL, {"type": "object"}, lambda session, **k: sent.append(k)
    )
    reg.register(send)
    reg.register(
        Tool(
            "x.combo",
            "x",
            SideEffect.DRAFT,
            {"type": "object"},
            lambda session, **k: sent.append(k),
            inner=("x.send",),
        )
    )
    pol = PolicyEngine({"autonomy": {"default": 2, "x.send": 2, "triage": 3}}, reg)
    combo = reg.call(session, pol, actor="r", name="x.combo", args={}, decision_type="triage")
    assert isinstance(combo.decision, NeedsApproval) and sent == []
    direct = reg.call(session, pol, actor="r", name="x.send", args={}, decision_type="triage")
    assert isinstance(direct.decision, NeedsApproval) and sent == []


def test_decision_type_level_applies_when_tool_has_no_setting(session):
    reg = ToolRegistry()
    reg.register(
        Tool("x.send", "x", SideEffect.EXTERNAL, {"type": "object"}, lambda session, **k: None)
    )
    pol = PolicyEngine({"autonomy": {"default": 2, "reply": 3}}, reg)
    res = reg.call(session, pol, actor="r", name="x.send", args={}, decision_type="reply")
    assert isinstance(res.decision, Allow)
