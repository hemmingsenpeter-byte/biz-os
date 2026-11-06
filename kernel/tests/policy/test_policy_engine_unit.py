"""Unit tests for PolicyEngine rules."""

from __future__ import annotations

from biz_kernel.approvals import autonomy
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, Allow, Deny, NeedsApproval, SideEffect


def act(tool="t.x", effect=SideEffect.EXTERNAL, **kw):
    return Action("role", tool, effect, **kw)


def test_hard_rules_deny(session):
    pol = PolicyEngine({"hard_rules": {"deny_tools": ["t.x"], "deny_decision_types": ["bad"]}})
    assert isinstance(pol.check(session, act()), Deny)
    assert isinstance(pol.check(session, act("t.y", decision_type="bad")), Deny)
    assert isinstance(pol.check(session, act("t.y", SideEffect.READ)), Allow)


def test_authority_and_levels(session):
    pol = PolicyEngine({"authority": {"pricing": "lead", "default": "boss"}})
    d = pol.check(session, act(decision_type="pricing"))
    assert isinstance(d, NeedsApproval) and d.approver == "lead"
    assert pol.check(session, act()).approver == "boss"
    autonomy.set_level(session, "role", "pricing", 4, "owner")
    assert isinstance(pol.check(session, act(decision_type="pricing")), Allow)


def test_money_limit(session):
    cfg = {"autonomy": {"default": 3}, "limits": {"max_amount": {"default": 100}}}
    pol = PolicyEngine(cfg)
    m = SideEffect.MONEY
    assert isinstance(pol.check(session, act("p", m, amount=50)), Allow)
    assert isinstance(pol.check(session, act("p", m, amount=500)), NeedsApproval)
    assert isinstance(pol.check(session, act("p", m)), NeedsApproval)
    cfg["limits"]["over_limit"] = "deny"
    assert isinstance(PolicyEngine(cfg).check(session, act("p", m, amount=500)), Deny)


def test_from_yaml(session, tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("autonomy:\n  default: 4\nhard_rules:\n  deny_tools: [t.no]\n")
    pol = PolicyEngine.from_yaml(p)
    assert isinstance(pol.check(session, act()), Allow)
    assert isinstance(pol.check(session, act("t.no")), Deny)
