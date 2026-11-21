"""Tool registry: typed functions registered by capabilities, gated by side-effect class."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import jsonschema
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.audit.log import record
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.control import service as control
from biz_kernel.policy import spend
from biz_kernel.policy.types import GATED, Action, Allow, Decision, Deny, SideEffect
from biz_kernel.tools.shadow import (
    SHADOW_REASON,
    ExecutionMode,
    Suppression,
    record_suppression,
    verdict,
)


@dataclass(frozen=True)
class Tool:
    name: str  # "<capability>.<verb>"
    capability: str
    side_effect: SideEffect
    schema: dict[str, Any]
    fn: Callable[..., Any]
    description: str = ""
    inner: tuple[str, ...] = ()  # tools a composite tool calls (strictest-gate rule)
    wants_actor: bool = False  # fn also receives the verified caller as `actor=`
    cost_usd: float | Callable[[dict[str, Any]], float] = 0  # per-call price, maybe from args

    def cost_for(self, args: dict[str, Any]) -> float:
        return float(self.cost_usd(args) if callable(self.cost_usd) else self.cost_usd)


class PolicyChecker(Protocol):
    def check(self, session: Session, action: Action) -> Decision: ...


class PolicyEvaluator(Protocol):
    """A checker that can also answer without side effects (no staging, counters or spend)."""

    def evaluate(self, session: Session, action: Action) -> Decision: ...


UNEVALUATED = {"decision": "unknown", "reason": "policy has no side-effect-free evaluation"}


@dataclass
class CallResult:
    decision: Decision
    output: Any = None
    suppression: Suppression | None = None

    @property
    def ran(self) -> bool:
        return isinstance(self.decision, Allow)


class NotAllowed(PermissionError):
    pass


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    budgets: BudgetBook | None = None  # set => paid tools are checked and charged per actor
    now: Callable[[], datetime] = db.utcnow
    execution_mode: ExecutionMode = ExecutionMode.SUPERVISED

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name} already registered")
        jsonschema.Draft202012Validator.check_schema(tool.schema)
        self.tools[tool.name] = tool

    def not_idempotent(self) -> list[str]:
        """Gated tools that take no idempotency_key (the runner passes a stable one)."""
        return sorted(
            t.name
            for t in self.tools.values()
            if t.side_effect in GATED and "idempotency_key" not in t.schema.get("properties", {})
        )

    def get(self, name: str) -> Tool:
        return self.tools[name]

    def search(self, query: str) -> list[Tool]:
        q = query.lower()
        return [t for t in self.tools.values() if q in t.name or q in t.description.lower()]

    def _gated_effects(self, name: str, parents: frozenset[str] = frozenset()) -> set[SideEffect]:
        tool = self.tools.get(name)
        if tool is None or name in parents:
            return {SideEffect.EXTERNAL}  # an unresolved composite cannot prove it is safe
        effects = {tool.side_effect} if tool.side_effect in GATED else set()
        for inner in tool.inner:
            effects.update(self._gated_effects(inner, parents | {name}))
        return effects

    def action(
        self, actor: str, name: str, args: dict[str, Any], decision_type: str | None = None
    ) -> Action:
        tool = self.get(name)
        return Action(
            actor=actor,
            tool=name,
            side_effect=tool.side_effect,
            inputs=args,
            decision_type=decision_type,
            inner=tool.inner,
            amount=args.get("amount"),
        )

    def suppress_in_shadow(
        self,
        session: Session,
        *,
        actor: str,
        name: str,
        args: dict[str, Any],
        decision_type: str | None = None,
        policy: PolicyChecker | None = None,
        pre_approved: bool = False,
        staged_op_id: str | None = None,
    ) -> CallResult | None:
        """In shadow mode, record a gated call instead of running it.

        The record carries what policy would have decided, from its side-effect-free
        evaluation: nothing is staged, counted, locked or spent. A checker without one
        is recorded as unknown rather than called.
        """
        if self.execution_mode != ExecutionMode.SHADOW:
            return None
        effects = self._gated_effects(name)
        if not effects:
            return None
        if control.is_paused(session):
            raise control.Paused(name)
        would_be = None
        if policy is not None:
            evaluate = getattr(policy, "evaluate", None)
            if callable(evaluate) and name in self.tools:
                would_be = verdict(evaluate(session, self.action(actor, name, args, decision_type)))
            else:
                would_be = dict(UNEVALUATED)
        suppression = record_suppression(
            session,
            actor,
            name,
            effects,
            args,
            decision_type,
            policy=would_be,
            pre_approved=pre_approved,
            staged_op_id=staged_op_id,
        )
        return CallResult(Deny(SHADOW_REASON), suppression=suppression)

    def call(
        self,
        session: Session,
        policy: PolicyChecker,
        *,
        actor: str,
        name: str,
        args: dict[str, Any],
        allow_list: set[str] | None = None,
        decision_type: str | None = None,
        pre_approved: bool = False,
    ) -> CallResult:
        """Run a tool. Gated tools pass policy.check unless the call replays a staged approval."""
        if allow_list is not None and name not in allow_list:
            raise NotAllowed(f"{actor} may not call {name}")
        tool = self.get(name)
        jsonschema.validate(args, tool.schema)
        if (tool.side_effect in GATED or tool.inner) and control.is_paused(session):
            # The kill switch outranks everything, including an approval being replayed.
            raise control.Paused(name)
        suppressed = self.suppress_in_shadow(
            session,
            actor=actor,
            name=name,
            args=args,
            decision_type=decision_type,
            policy=policy,
            pre_approved=pre_approved,
        )
        if suppressed is not None:
            return suppressed
        cost = tool.cost_for(args)
        metered = cost > 0 and self.budgets is not None
        if metered:  # approval never bypasses the budget: checked before policy and execution
            assert self.budgets is not None
            # The cost is known up front, so a call that would overshoot never starts.
            self.budgets.preflight(session, actor, cost, self.now())
        decision: Decision = Allow("ungated")
        if tool.side_effect in GATED or tool.inner:
            action = self.action(actor, name, args, decision_type)
            decision = Allow("pre-approved") if pre_approved else policy.check(session, action)
            record(
                session,
                actor,
                "policy",
                name,
                decision=type(decision).__name__,
                reason=getattr(decision, "reason", ""),
            )
        if not isinstance(decision, Allow):
            return CallResult(decision)
        if "actor" in args:
            raise ValueError("`actor` is set by the kernel, never by the caller")
        extra = {"actor": actor} if tool.wants_actor else {}
        charged = None
        if metered:  # reserve under the role's lock (held to commit), so no race overshoots
            assert self.budgets is not None
            charged = self.budgets.charge(session, actor, cost, "tool", name, self.now())
        try:
            if tool.side_effect in GATED or tool.inner:
                control.admit(session, name)
            output = tool.fn(session=session, **extra, **args)
        except Exception:
            if charged is not None and self.budgets is not None:
                # A failed call is not charged. If the refund itself fails, the transaction
                # is failing anyway and its rollback drops the charge.
                with contextlib.suppress(SQLAlchemyError):
                    self.budgets.refund(session, charged)
            raise
        record(session, actor, "tool_call", name, args=args)
        if self.moves_money(name) and args.get("amount") is not None:
            # Feeds limits.max_daily. Policy already refused an unusable amount.
            spend.add(
                session,
                actor=actor,
                tool=name,
                decision_type=decision_type,
                amount=Decimal(str(args["amount"])),
                now=self.now(),
            )
        return CallResult(decision, output)

    def moves_money(self, name: str, depth: int = 0) -> bool:
        """The tool, or any tool it wraps, is a money action."""
        tool = self.tools.get(name)
        if tool is None or depth > 8:
            return False
        return tool.side_effect == SideEffect.MONEY or any(
            self.moves_money(i, depth + 1) for i in tool.inner
        )
