"""Suggestions: proposals from planner and learning. Approving stages a pre-approved operation."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation, Suggestion
from biz_kernel.audit.log import record
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.tools.registry import ToolRegistry


def propose(
    session: Session,
    source: str,
    title: str,
    tool: str,
    inputs: dict[str, Any],
    reasons: list[str],
    evidence: dict[str, Any] | None = None,
    decision_type: str | None = None,
) -> Suggestion:
    """Record a suggestion. Nothing executes."""
    s = Suggestion(
        source=source,
        title=title,
        tool=tool,
        inputs=staged._json(inputs),
        reasons=list(reasons),
        evidence=staged._json(evidence or {}),
        decision_type=decision_type,
    )
    session.add(s)
    session.flush()
    record(session, source, "suggestion", str(s.id), event="proposed", tool=tool, title=title)
    return s


def approve_suggestion(
    session: Session, suggestion_id: uuid.UUID, by: str, registry: ToolRegistry | None = None
) -> StagedOperation:
    """Create a staged operation pre-approved by `by` (owner). Still needs execute()."""
    s = session.get(Suggestion, suggestion_id, with_for_update=True)
    if s is None or s.status != "proposed":
        raise staged.ApprovalError("suggestion is not open")
    side_effect = registry.get(s.tool).side_effect if registry else SideEffect.EXTERNAL
    action = Action(
        actor=s.source,
        tool=s.tool,
        side_effect=side_effect,
        inputs=s.inputs,
        decision_type=s.decision_type,
        amount=s.inputs.get("amount"),
    )
    op = staged.stage(session, action, approver=by, reason=f"suggestion: {s.title}")
    op = staged.approve(session, op.id, by=by, version=op.version)
    s.status, s.staged_op_id = "approved", op.id
    session.flush()
    record(session, by, "suggestion", str(s.id), event="approved", staged_op_id=str(op.id))
    return op


def reject_suggestion(session: Session, suggestion_id: uuid.UUID, by: str) -> Suggestion:
    s = session.get(Suggestion, suggestion_id, with_for_update=True)
    if s is None or s.status != "proposed":
        raise staged.ApprovalError("suggestion is not open")
    s.status = "rejected"
    session.flush()
    record(session, by, "suggestion", str(s.id), event="rejected")
    return s
