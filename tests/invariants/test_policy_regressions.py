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


# -- Kernel review #1: money fails closed on bad amounts and bad limits.

HIVE_LIMITS = {"max_amount": {"default": 0}, "over_limit": "deny"}
APPROVAL_LIMITS = {"max_amount": {"default": 500}}  # over the limit needs approval
BAD_AMOUNTS: list = [-1, -0.01, float("nan"), float("inf"), float("-inf"), "5", True]


def test_bad_money_amount_is_denied_at_every_level(session):
    paid: list[dict] = []
    reg = ToolRegistry()
    reg.register(
        Tool(
            "x.pay", "x", SideEffect.MONEY, {"type": "object"}, lambda session, **k: paid.append(k)
        )
    )
    for limits in (HIVE_LIMITS, LIMITS, APPROVAL_LIMITS):
        for level in (1, 2, 3, 4):
            pol = PolicyEngine({"autonomy": {"default": level}, "limits": limits}, reg)
            for amount in BAD_AMOUNTS:
                act = Action("r", "x.pay", SideEffect.MONEY, amount=amount)
                assert isinstance(pol.check(session, act), Deny), (limits, level, amount)
                res = staged.run_or_stage(session, reg, pol, "r", "x.pay", {"amount": amount})
                assert not isinstance(res, staged.StagedOperation), (limits, level, amount)
    assert paid == []
    pol = PolicyEngine({"autonomy": {"default": 3}, "limits": HIVE_LIMITS}, reg)
    zero = Action("r", "x.pay", SideEffect.MONEY, amount=0)
    assert isinstance(pol.check(session, zero), Allow)  # a valid zero amount still passes


def test_bad_money_limit_in_config_fails_closed(session):
    reg = ToolRegistry()
    act = Action("r", "x.pay", SideEffect.MONEY, amount=1)
    for bad in (float("nan"), -1, float("inf"), "lots", True):
        for per in ({"default": bad}, {"x.pay": bad, "default": 1000}, bad):
            pol = PolicyEngine({"autonomy": {"default": 4}, "limits": {"max_amount": per}}, reg)
            assert isinstance(pol.check(session, act), Deny), per


def test_composite_with_inner_money_tool_denies_bad_amount(session):
    paid: list[dict] = []
    reg = ToolRegistry()
    reg.register(Tool("x.pay", "x", SideEffect.MONEY, AMOUNT, lambda session, **k: paid.append(k)))
    reg.register(
        Tool(
            "x.combo",
            "x",
            SideEffect.DRAFT,
            {"type": "object"},
            lambda session, **k: paid.append(k),
            inner=("x.pay",),
        )
    )
    for limits in (HIVE_LIMITS, APPROVAL_LIMITS):
        pol = PolicyEngine({"autonomy": {"default": 4}, "limits": limits}, reg)
        for amount in (-50, float("nan"), float("-inf")):
            res = reg.call(session, pol, actor="r", name="x.combo", args={"amount": amount})
            assert isinstance(res.decision, Deny), (limits, amount)
    assert paid == []
