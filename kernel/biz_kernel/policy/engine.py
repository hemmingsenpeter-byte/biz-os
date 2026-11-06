"""Policy engine: hard rules, limits, authority and the autonomy ladder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from biz_kernel.approvals import autonomy
from biz_kernel.policy.types import (
    GATED,
    Action,
    Allow,
    Decision,
    Deny,
    NeedsApproval,
    SideEffect,
    strictest,
)
from biz_kernel.tools.registry import ToolRegistry

DEFAULT_APPROVER = "owner"
DEFAULT_LEVEL = 2  # unknown decision types recommend; they never act alone
MAX_DEPTH = 8


class PolicyEngine:
    """config keys: limits, authority, hard_rules, autonomy (see docs/SPEC.md)."""

    def __init__(self, config: dict[str, Any] | None = None, registry: ToolRegistry | None = None):
        cfg = config or {}
        self.limits: dict[str, Any] = cfg.get("limits") or {}
        self.authority: dict[str, str] = cfg.get("authority") or {}
        self.hard_rules: dict[str, Any] = cfg.get("hard_rules") or {}
        self.autonomy: dict[str, int] = cfg.get("autonomy") or {}
        self.registry = registry

    @classmethod
    def from_yaml(cls, path: str | Path, registry: ToolRegistry | None = None) -> PolicyEngine:
        return cls(yaml.safe_load(Path(path).read_text()) or {}, registry)

    # -- helpers
    def approver_for(self, action: Action) -> str:
        for key in (action.tool, action.decision_type):
            if key and key in self.authority:
                return self.authority[key]
        return self.authority.get("default", DEFAULT_APPROVER)

    def _limit(self, action: Action) -> float | None:
        per = self.limits.get("max_amount")
        if isinstance(per, dict):
            for key in (action.tool, action.decision_type, "default"):
                if key and key in per:
                    return float(per[key])
            return None
        return float(per) if per is not None else None

    def _hard_deny(self, action: Action) -> Deny | None:
        if action.tool in (self.hard_rules.get("deny_tools") or []):
            return Deny(f"hard rule: {action.tool} is denied")
        if action.decision_type and action.decision_type in (
            self.hard_rules.get("deny_decision_types") or []
        ):
            return Deny(f"hard rule: decision type {action.decision_type} is denied")
        return None

    def _level(self, session: Session, action: Action) -> int:
        """Strictest (lowest) level among the tool's own key and the SOP's decision type.

        A decision type can never loosen a tool's own setting; with neither configured
        nor stored, the default applies.
        """
        levels = []
        for key in (action.tool, action.decision_type):
            if not key:
                continue
            configured = self.autonomy.get(key)
            stored = autonomy.level(session, action.actor, key, -1)
            if stored != -1:
                levels.append(stored)
            elif configured is not None:
                levels.append(int(configured))
        if levels:
            return min(levels)
        return int(self.autonomy.get("default", DEFAULT_LEVEL))

    def _own(self, session: Session, action: Action) -> Decision:
        # Order matters: hard rules, then money limits (a deny limit applies at every
        # autonomy level, so an approval can never authorise it), then the ladder.
        if (deny := self._hard_deny(action)) is not None:
            return deny
        if action.side_effect not in GATED:
            return Allow("ungated")
        approver = self.approver_for(action)
        over_limit: NeedsApproval | None = None
        if action.side_effect == SideEffect.MONEY:
            limit = self._limit(action)
            if limit is None:
                over_limit = NeedsApproval(approver, "no money limit configured")  # fail closed
            elif action.amount is None:
                over_limit = NeedsApproval(approver, "money action without an amount")
            elif action.amount > limit:
                if self.limits.get("over_limit", "approval") == "deny":
                    return Deny(f"amount {action.amount} is over the limit {limit}")
                over_limit = NeedsApproval(
                    approver, f"amount {action.amount} is over the limit {limit}"
                )
        lvl = self._level(session, action)
        if lvl <= 2:
            return NeedsApproval(approver, f"autonomy level {lvl} requires approval")
        if over_limit is not None:
            return over_limit
        return Allow(f"autonomy level {lvl}")

    def _inner_effect(self, name: str) -> tuple[SideEffect, tuple[str, ...]]:
        if self.registry is not None and name in self.registry.tools:
            tool = self.registry.get(name)
            return tool.side_effect, tool.inner
        return SideEffect.EXTERNAL, ()  # unknown inner tool: assume the strictest class

    def _collect(self, session: Session, action: Action, depth: int) -> list[Decision]:
        out = [self._own(session, action)]
        if depth > MAX_DEPTH:
            return [Deny("composite nesting too deep")]
        for name in action.inner:
            effect, deeper = self._inner_effect(name)
            sub = Action(
                actor=action.actor,
                tool=name,
                side_effect=effect,
                inputs=action.inputs,
                decision_type=None,  # inner tools are judged by their own setting
                inner=deeper,
                amount=action.amount,
            )
            out.extend(self._collect(session, sub, depth + 1))
        return out

    def check(self, session: Session, action: Action) -> Decision:
        """Strictest of the action's own gate and every inner tool's gate."""
        return strictest(self._collect(session, action, 0))
