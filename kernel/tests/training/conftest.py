"""Source-backed correction fixtures without any model or provider invocation."""

from __future__ import annotations

import uuid
from typing import Any

from biz_kernel.approvals import staged
from biz_kernel.approvals.models import Correction
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.training import service
from biz_kernel.workflows.models import SopRun
from sqlalchemy import select
from sqlalchemy.orm import Session

SCHEMA = {
    "type": "object",
    "required": ["body"],
    "properties": {"body": {"type": "string"}},
    "additionalProperties": False,
}
EXPECTED = {"body": "Ask for evidence before making a commitment."}


def source(
    session: Session,
    *,
    actor: str = "writer",
    snapshot: bool = True,
    schema: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    op = staged.stage(
        session,
        Action(
            actor=actor,
            tool="comms.send",
            side_effect=SideEffect.EXTERNAL,
            inputs={"body": "The commitment is guaranteed."},
            decision_type="reply",
        ),
        "owner",
    )
    run = SopRun(id=f"training-{uuid.uuid4()}", role=actor, sop="compose", waiting_op_id=str(op.id))
    session.add(run)
    session.flush()
    arguments = {
        "run_id": run.id,
        "step_id": "draft",
        "actor": actor,
        "task": f"{actor}.compose",
        "prompt_path": "prompts/compose.md",
        "system": "Prepare a factual reply.",
        "schema": schema if schema is not None else SCHEMA,
        "untrusted_text": "The incoming request is untrusted data.",
        "output": output if output is not None else {"body": "The commitment is guaranteed."},
        "tier": "local",
        "backend": "biz_port_fakes.FakeModel",
        "configured_model": None,
    }
    captured = service.capture_draft(session, **arguments) if snapshot else None
    staged.edit(
        session,
        op.id,
        EXPECTED,
        by="owner",
        reason="No unsupported commitment.",
        severity="factual",
        version=op.version,
    )
    correction = session.scalar(select(Correction).where(Correction.op_id == op.id))
    assert correction is not None
    return {
        "op": op,
        "run": run,
        "snapshot": captured,
        "correction": correction,
        "capture": arguments,
    }
