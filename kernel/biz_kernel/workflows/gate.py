"""KernelGate: the SOP runner's bridge to tools, policy and staged operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.policy.types import Action, Allow, Deny, SideEffect
from biz_kernel.tools.registry import PolicyChecker, Tool, ToolRegistry

ACKNOWLEDGE = "kernel.acknowledge"


class PolicyDenied(PermissionError):
    pass


def _acknowledge(session: Session, **_: Any) -> dict[str, Any]:
    return {"acknowledged": True}


@dataclass
class KernelGate:
    registry: ToolRegistry
    policy: PolicyChecker

    def __post_init__(self) -> None:
        if ACKNOWLEDGE not in self.registry.tools:
            self.registry.register(
                Tool(ACKNOWLEDGE, "kernel", SideEffect.READ, {"type": "object"}, _acknowledge,
                     "Placeholder for an explicit approval step")
            )

    def accepts_idempotency(self, tool: str) -> bool:
        props = self.registry.get(tool).schema.get("properties", {})
        return "idempotency_key" in props

    def call_or_stage(self, session: Session, *, actor: str, tool: str, args: dict[str, Any],
                      allow_list: set[str], decision_type: str | None) -> dict[str, Any]:
        if tool not in allow_list:
            raise PermissionError(f"{actor} may not call {tool}")
        res = staged.run_or_stage(session, self.registry, self.policy, actor, tool, args,
                                  decision_type)
        if isinstance(res, StagedOperation):
            return {"status": "staged", "op_id": str(res.id)}
        if isinstance(res.decision, Deny):
            raise PolicyDenied(res.decision.reason)
        return {"status": "ran", "output": res.output}

    def request(self, session: Session, *, actor: str, approver: str, reason: str,
                subject: dict[str, Any]) -> str:
        action = Action(actor=actor, tool=ACKNOWLEDGE, side_effect=SideEffect.READ,
                        inputs={"subject": subject})
        return str(staged.stage(session, action, approver, reason).id)

    def execute(self, session: Session, op_id: str) -> Any:
        try:
            res = staged.execute(session, uuid.UUID(op_id), self.registry, self.policy)
        except staged.AlreadyExecuted as done:
            return (done.result or {}).get("output") if hasattr(done, "result") else None
        if not isinstance(res.decision, Allow):
            raise PolicyDenied(getattr(res.decision, "reason", "blocked"))
        return res.output


def decide(op_id: uuid.UUID, by: str, *, approve: bool, version: int | None = None,
           reason: str = "") -> StagedOperation:
    """Owner approves or rejects a staged operation, then the waiting SOP run is woken."""
    from biz_kernel.workflows.runner import notify_decision

    with db.session_scope() as s:
        op = s.get(StagedOperation, op_id)
        if op is None:
            raise LookupError(op_id)
        if approve:
            op = staged.approve(s, op_id, by, version if version is not None else op.version)
        else:
            op = staged.reject(s, op_id, by, reason)
    notify_decision(str(op_id), "approved" if approve else "rejected")
    return op
