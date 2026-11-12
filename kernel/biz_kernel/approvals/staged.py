"""Staged operations: exact-input approval, version pinning, run exactly once."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.approvals import autonomy
from biz_kernel.approvals.models import SEVERITIES, Correction, StagedOperation
from biz_kernel.audit.log import record
from biz_kernel.control import service as control
from biz_kernel.db import utcnow
from biz_kernel.policy.types import Action, Deny, NeedsApproval
from biz_kernel.tools.registry import CallResult, PolicyChecker, ToolRegistry

OWNER = "owner"
IDEMPOTENCY_KEY = "idempotency_key"


class ApprovalError(Exception):
    pass


class StaleVersion(ApprovalError):
    pass


class NotApproved(ApprovalError):
    pass


class NotAuthorised(ApprovalError):
    pass


class AlreadyExecuted(ApprovalError):
    def __init__(self, op: StagedOperation) -> None:
        super().__init__(f"operation {op.id} already executed")
        self.op = op
        self.result = op.result


def digest(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _locked(session: Session, op_id: uuid.UUID) -> StagedOperation:
    op = session.scalar(
        select(StagedOperation)
        .where(StagedOperation.id == op_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if op is None:
        raise ApprovalError(f"no staged operation {op_id}")
    return op


def stage(session: Session, action: Action, approver: str, reason: str = "") -> StagedOperation:
    inputs = _json(action.inputs)
    op = StagedOperation(
        actor=action.actor,
        tool=action.tool,
        inputs=inputs,
        inputs_sha256=digest(inputs),
        version=1,
        approver=approver,
        status="pending",
        decision_type=action.decision_type,
        reason=reason,
    )
    session.add(op)
    session.flush()
    record(
        session,
        action.actor,
        "approval",
        str(op.id),
        event="staged",
        tool=op.tool,
        approver=approver,
        version=1,
        sha256=op.inputs_sha256,
    )
    return op


def approve(session: Session, op_id: uuid.UUID, by: str, version: int) -> StagedOperation:
    """Approve exactly `version`. The approver (or owner) may approve; never the actor."""
    op = _locked(session, op_id)
    if op.status != "pending":
        raise ApprovalError(f"operation is {op.status}, not pending")
    if version != op.version:
        raise StaleVersion(f"approval names version {version}, current is {op.version}")
    if by == op.actor or by not in (op.approver, OWNER):
        raise NotAuthorised(f"{by} may not approve this operation")
    control.admit(session, "approval")
    op.status, op.approved_by = "approved", by
    op.approved_version, op.approved_sha256 = op.version, op.inputs_sha256
    if op.decision_type:
        autonomy.record_approval(session, op.actor, op.decision_type, unchanged=op.version == 1)
    session.flush()
    record(
        session,
        by,
        "approval",
        str(op.id),
        event="approved",
        version=op.version,
        sha256=op.approved_sha256,
    )
    return op


def edit(
    session: Session,
    op_id: uuid.UUID,
    new_inputs: dict[str, Any],
    by: str = "",
    reason: str = "",
    severity: str | None = None,
    *,
    version: int,
) -> StagedOperation:
    """Change inputs: bumps the version and clears any approval.

    An edit by anyone but the proposing role is a correction: the draft, the edit, the
    reason and its severity (cosmetic, factual or policy) are kept as training data.

    The operation's idempotency key is not the editor's to change: every version carries
    the original key, so the send ledgers dedupe and an edit can never cause a second send.
    """
    if severity is not None and severity not in SEVERITIES:
        raise ApprovalError(f"severity must be one of {', '.join(SEVERITIES)}")
    op = _locked(session, op_id)
    if version != op.version:
        raise StaleVersion(f"edit names version {version}, current is {op.version}")
    if op.status not in ("pending", "approved"):
        raise ApprovalError(f"operation is {op.status}, cannot edit")
    inputs = _json(new_inputs)
    if IDEMPOTENCY_KEY in op.inputs:
        inputs[IDEMPOTENCY_KEY] = op.inputs[IDEMPOTENCY_KEY]
    if by and by != op.actor and inputs != op.inputs:
        session.add(
            Correction(
                op_id=op.id,
                actor=op.actor,
                tool=op.tool,
                decision_type=op.decision_type,
                from_version=op.version,
                to_version=op.version + 1,
                before=op.inputs,
                after=inputs,
                by=by,
                reason=reason,
                severity=severity,
            )
        )
    op.inputs, op.inputs_sha256 = inputs, digest(inputs)
    op.version += 1
    op.status = "pending"
    op.approved_by = op.approved_version = op.approved_sha256 = None
    session.flush()
    record(
        session,
        by or op.actor,
        "approval",
        str(op.id),
        event="edited",
        version=op.version,
        sha256=op.inputs_sha256,
        reason=reason,
        severity=severity,
    )
    return op


def reject(session: Session, op_id: uuid.UUID, by: str, reason: str = "") -> StagedOperation:
    op = _locked(session, op_id)
    if op.status in ("executed", "rejected"):
        raise ApprovalError(f"operation is {op.status}")
    op.status = "rejected"
    op.approved_by = op.approved_version = op.approved_sha256 = None
    session.flush()
    record(session, by, "approval", str(op.id), event="rejected", reason=reason)
    return op


def execute(
    session: Session, op_id: uuid.UUID, registry: ToolRegistry, policy: PolicyChecker
) -> CallResult:
    """Run exactly the approved inputs, once. A second call raises AlreadyExecuted."""
    op = _locked(session, op_id)
    if op.status == "executed":
        raise AlreadyExecuted(op)
    if op.status != "approved" or op.approved_version != op.version:
        raise NotApproved(f"operation is {op.status}")
    if digest(op.inputs) != op.inputs_sha256 or op.inputs_sha256 != op.approved_sha256:
        raise NotApproved("inputs do not match the approved version")
    action = Action(
        actor=op.actor,
        tool=op.tool,
        side_effect=registry.get(op.tool).side_effect,
        inputs=op.inputs,
        decision_type=op.decision_type,
        inner=registry.get(op.tool).inner,
        amount=op.inputs.get("amount"),
    )
    verdict = policy.check(session, action)  # approval satisfies the gate, not a hard rule
    if isinstance(verdict, Deny):
        record(session, op.actor, "approval", str(op.id), event="blocked", reason=verdict.reason)
        return CallResult(verdict)
    result = registry.call(
        session,
        policy,
        actor=op.actor,
        name=op.tool,
        args=dict(op.inputs),
        decision_type=op.decision_type,
        pre_approved=True,
    )
    op.status, op.executed_at = "executed", utcnow()
    op.result = _json({"output": result.output})
    session.flush()
    record(
        session,
        op.actor,
        "approval",
        str(op.id),
        event="executed",
        version=op.version,
        sha256=op.inputs_sha256,
    )
    return result


def run_or_stage(
    session: Session,
    registry: ToolRegistry,
    policy: PolicyChecker,
    actor: str,
    name: str,
    args: dict[str, Any],
    decision_type: str | None = None,
) -> CallResult | StagedOperation:
    """Call the tool; if policy wants approval, stage it instead and return the operation."""
    result = registry.call(
        session, policy, actor=actor, name=name, args=args, decision_type=decision_type
    )
    if isinstance(result.decision, NeedsApproval):
        tool = registry.get(name)
        action = Action(
            actor=actor,
            tool=name,
            side_effect=tool.side_effect,
            inputs=args,
            decision_type=decision_type,
            inner=tool.inner,
            amount=args.get("amount"),
        )
        return stage(session, action, result.decision.approver, result.decision.reason)
    return result


def corrections(session: Session, limit: int = 100) -> list[Correction]:
    """Newest first."""
    return list(session.scalars(select(Correction).order_by(Correction.id.desc()).limit(limit)))


def pending(session: Session, approver: str | None = None) -> list[StagedOperation]:
    """Approval queue: operations awaiting approval (or approved but not yet run)."""
    q = select(StagedOperation).where(StagedOperation.status.in_(("pending", "approved")))
    if approver:
        q = q.where(StagedOperation.approver == approver)
    return list(session.scalars(q.order_by(StagedOperation.created_at)))


__all__ = [
    "AlreadyExecuted",
    "ApprovalError",
    "NotApproved",
    "StaleVersion",
    "approve",
    "corrections",
    "datetime",
    "digest",
    "edit",
    "execute",
    "pending",
    "reject",
    "run_or_stage",
    "stage",
]
