"""Tool registry: typed functions registered by capabilities, gated by side-effect class."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
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
        output = tool.fn(session=session, **extra, **args)
        record(session, actor, "tool_call", name, args=args)
        return CallResult(decision, output)
