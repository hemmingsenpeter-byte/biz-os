"""Tool registry: typed functions registered by capabilities, gated by side-effect class."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import jsonschema
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.audit.log import record
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.policy.types import GATED, Action, Allow, Decision, SideEffect


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


@dataclass
class CallResult:
    decision: Decision
    output: Any = None

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
        cost = tool.cost_for(args)
        metered = cost > 0 and self.budgets is not None
        if metered:  # approval never bypasses the budget: checked before policy and execution
            assert self.budgets is not None
            # The cost is known up front, so a call that would overshoot never starts.
            self.budgets.preflight(session, actor, cost, self.now())
        decision: Decision = Allow("ungated")
        if tool.side_effect in GATED or tool.inner:
            action = Action(
                actor=actor,
                tool=name,
                side_effect=tool.side_effect,
                inputs=args,
                decision_type=decision_type,
                inner=tool.inner,
                amount=args.get("amount"),
            )
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
            output = tool.fn(session=session, **extra, **args)
        except Exception:
            if charged is not None and self.budgets is not None:
                # A failed call is not charged. If the refund itself fails, the transaction
                # is failing anyway and its rollback drops the charge.
                with contextlib.suppress(SQLAlchemyError):
                    self.budgets.refund(session, charged)
            raise
        record(session, actor, "tool_call", name, args=args)
        return CallResult(decision, output)
