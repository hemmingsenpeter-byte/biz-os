"""Policy engine: hard rules, limits, authority and the autonomy ladder."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from sqlalchemy.orm import Session

from biz_kernel.approvals import autonomy
from biz_kernel.db import utcnow
from biz_kernel.policy import spend
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


class BadMoney(ValueError):
    """A money amount or limit that is not a finite, non-negative number."""


def _money(value: Any, what: str) -> float:
    """A finite, non-negative number, or BadMoney. Booleans and strings are not amounts."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise BadMoney(f"money {what} {value!r} is not a number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise BadMoney(f"money {what} {value!r} is not a usable number") from None
    if not math.isfinite(number) or number < 0:
        raise BadMoney(f"money {what} {value!r} must be finite and not negative")
    return number


def limit_problems(limits: dict[str, Any]) -> list[str]:
    """Config problems in `limits` (biz doctor); at runtime each one is a Deny."""
    out = []
    for name in ("max_amount", "max_daily"):
        per = limits.get(name)
        values = per if isinstance(per, dict) else {} if per is None else {"": per}
        for key, value in values.items():
            try:
                _money(value, "limit")
            except BadMoney as exc:
                out.append(f"limits.{name}{'.' + key if key else ''}: {exc}")
    tz = limits.get("day_timezone", "UTC")
    try:
        ZoneInfo(tz)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        out.append(f"limits.day_timezone {tz!r} is not a known time zone")
    if limits.get("over_limit", "approval") not in ("approval", "deny"):
        out.append("limits.over_limit must be approval or deny")
    return out


class PolicyEngine:
    """config keys: limits, authority, hard_rules, autonomy (see docs/SPEC.md)."""

    def __init__(self, config: dict[str, Any] | None = None, registry: ToolRegistry | None = None):
        cfg = config or {}
        self.limits: dict[str, Any] = cfg.get("limits") or {}
        self.authority: dict[str, str] = cfg.get("authority") or {}
        self.hard_rules: dict[str, Any] = cfg.get("hard_rules") or {}
        self.autonomy: dict[str, int] = cfg.get("autonomy") or {}
        self.registry = registry
        self.now: Callable[[], datetime] = utcnow

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
        """The configured limit, None if there is none. Raises BadMoney for an unusable one."""
        per = self.limits.get("max_amount")
        if isinstance(per, dict):
            for key in (action.tool, action.decision_type, "default"):
                if key and key in per:
                    return _money(per[key], "limit")
            return None
        return _money(per, "limit") if per is not None else None

    def _daily(self, session: Session, action: Action) -> tuple[float, Decimal] | None:
        """(daily limit, spent so far today) or None. Raises BadMoney for an unusable limit.

        `limits.max_daily` is keyed like max_amount. A tool or decision-type key counts only
        that key's spend; `default` (or a bare number) counts every money action.
        """
        per = self.limits.get("max_daily")
        if per is None:
            return None
        narrow: dict[str, str | None] = {}
        if isinstance(per, dict):
            if action.tool in per:
                raw, narrow = per[action.tool], {"tool": action.tool}
            elif action.decision_type and action.decision_type in per:
                raw, narrow = per[action.decision_type], {"decision_type": action.decision_type}
            elif "default" in per:
                raw = per["default"]
            else:
                return None
        else:
            raw = per
        limit = _money(raw, "daily limit")
        tz = self.limits.get("day_timezone", "UTC")
        try:
            ZoneInfo(tz)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            raise BadMoney(f"day_timezone {tz!r} is not a known time zone") from None
        spend.lock(session)  # held to commit, so the check and the ledger insert can't race
        return limit, spend.spent_today(session, self.now(), tz, **narrow)

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
        nor stored, the default applies. A key named in policy.yaml is an upper bound: a stored
        level (owner-set, promoted or demoted) can only be stricter, so lowering the file
        always takes effect.
        """
        levels = []
        for key in (action.tool, action.decision_type):
            if not key:
                continue
            configured = self.autonomy.get(key)
            stored = autonomy.level(session, action.actor, key, -1)
            if configured is not None:
                levels.append(int(configured) if stored == -1 else min(stored, int(configured)))
            elif stored != -1:
                levels.append(stored)
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
            # Fail closed: a negative, NaN, infinite or non-numeric amount or limit would
            # slip past `amount > limit`, so it is a Deny that no approval can override.
            try:
                limit = self._limit(action)
                amount = None if action.amount is None else _money(action.amount, "amount")
            except BadMoney as exc:
                return Deny(str(exc))
            if limit is None:
                over_limit = NeedsApproval(approver, "no money limit configured")  # fail closed
            elif amount is None:
                over_limit = NeedsApproval(approver, "money action without an amount")
            elif amount > limit:
                if self.limits.get("over_limit", "approval") == "deny":
                    return Deny(f"amount {action.amount} is over the limit {limit}")
                over_limit = NeedsApproval(
                    approver, f"amount {action.amount} is over the limit {limit}"
                )
            try:
                daily = self._daily(session, action)
            except BadMoney as exc:
                return Deny(str(exc))
            if daily is not None and amount is not None:
                day_limit, spent = daily
                if float(spent) + amount > day_limit:
                    why = f"amount {action.amount} takes today's total {spent} over {day_limit}"
                    if self.limits.get("over_limit", "approval") == "deny":
                        return Deny(why)
                    over_limit = over_limit or NeedsApproval(approver, why)
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
