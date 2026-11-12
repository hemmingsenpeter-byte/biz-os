"""Approval queue routes. Staff view; only the owner or the named approver decides."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.auth.models import User
from biz_kernel.control import service as control
from biz_kernel.workflows.models import SopRun, SopStep

Decide = Callable[..., Any]


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    actor: str
    tool: str
    inputs: dict[str, Any]
    version: int
    approver: str
    status: str
    approved_by: str | None
    reason: str
    # Additive: where the owner can open this approval and the work it belongs to.
    run_id: str | None = None
    task_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    links: dict[str, str] = Field(default_factory=dict)


class ApproveIn(BaseModel):
    version: int


class RejectIn(BaseModel):
    reason: str = ""


class EditIn(BaseModel):
    version: int
    inputs: dict[str, Any]
    reason: str = ""  # one line: why the draft was wrong
    severity: Literal["cosmetic", "factual", "policy"] | None = None


class CorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    op_id: uuid.UUID
    actor: str
    tool: str
    decision_type: str | None
    from_version: int
    to_version: int
    before: dict[str, Any]
    after: dict[str, Any]
    by: str
    reason: str
    severity: str | None


def identity(user: User) -> str:
    """The name a user decides under: the owner is `owner`, anyone else their email."""
    return staged.OWNER if user.role == "owner" else user.email


def _load(session: Session, op_id: uuid.UUID) -> StagedOperation:
    op = session.get(StagedOperation, op_id)
    if op is None:
        raise HTTPException(404, "no such approval")
    return op


def _run_for(session: Session, op: StagedOperation) -> SopRun | None:
    """The SOP run that staged or waits on this operation, if any."""
    oid = str(op.id)
    run = session.scalar(select(SopRun).where(SopRun.waiting_op_id == oid))
    if run is not None:
        return run
    step = session.scalar(
        select(SopStep)
        .where(
            or_(
                and_(
                    SopStep.step_id.like("%:staged"),
                    SopStep.output.contains({"op_id": oid}),
                ),
                and_(
                    SopStep.step_id.like("%:request"),
                    SopStep.output == literal(oid, JSONB),
                ),
            )
        )
        .limit(1)
    )
    return session.get(SopRun, step.run_id) if step is not None else None


def card(session: Session, op: StagedOperation) -> ApprovalOut:
    """An approval as the owner sees it, linked to itself and to its run, task and job."""
    out = ApprovalOut.model_validate(op)
    links = {"self": f"/v1/approvals/{op.id}"}
    run = _run_for(session, op)
    if run is not None:
        out.run_id, out.task_id, out.job_id = run.id, run.task_id, run.job_id
        if run.task_id is not None:
            links["task"] = f"/v1/tasks/{run.task_id}"
        if run.job_id is not None:
            links["job"] = f"/v1/jobs/{run.job_id}/timeline"
    out.links = links
    return out


def _may_decide(user: User, op: StagedOperation) -> None:
    if user.role != "owner" and identity(user) != op.approver:
        raise HTTPException(403, "only the owner or the named approver may decide")


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, staged.StaleVersion):
        return HTTPException(409, str(exc))
    if isinstance(exc, staged.NotAuthorised):
        return HTTPException(403, str(exc))
    return HTTPException(409, str(exc))


def router() -> APIRouter:
    r = APIRouter(prefix="/approvals", tags=["approvals"])

    @r.get("", response_model=list[ApprovalOut])
    def queue(_: CurrentUser, session: DbSession) -> list[ApprovalOut]:
        return [card(session, op) for op in staged.pending(session)]

    @r.get("/{op_id}", response_model=ApprovalOut)
    def one(op_id: uuid.UUID, _: CurrentUser, session: DbSession) -> ApprovalOut:
        return card(session, _load(session, op_id))

    @r.post("/{op_id}/approve", response_model=ApprovalOut)
    def approve(
        op_id: uuid.UUID, body: ApproveIn, user: CurrentUser, session: DbSession, request: Request
    ) -> ApprovalOut:
        op = _load(session, op_id)
        _may_decide(user, op)
        if control.is_paused(session):
            raise HTTPException(409, "paused: resume before approving")
        if op.status != "pending":
            raise HTTPException(409, f"operation is {op.status}, not pending")
        if body.version != op.version:
            raise HTTPException(409, f"current version is {op.version}")
        decide: Decide = request.app.state.decide
        try:
            decide(op_id, identity(user), approve=True, version=body.version)
        except (staged.ApprovalError, LookupError) as exc:
            raise _translate(exc) from exc
        session.expire_all()
        return card(session, _load(session, op_id))

    @r.post("/{op_id}/reject", response_model=ApprovalOut)
    def reject(
        op_id: uuid.UUID, body: RejectIn, user: CurrentUser, session: DbSession, request: Request
    ) -> ApprovalOut:
        op = _load(session, op_id)
        _may_decide(user, op)
        decide: Decide = request.app.state.decide
        try:
            decide(op_id, identity(user), approve=False, reason=body.reason)
        except (staged.ApprovalError, LookupError) as exc:
            raise _translate(exc) from exc
        session.expire_all()
        return card(session, _load(session, op_id))

    @r.post("/{op_id}/edit", response_model=ApprovalOut)
    def edit(op_id: uuid.UUID, body: EditIn, user: CurrentUser, session: DbSession) -> ApprovalOut:
        _may_decide(user, _load(session, op_id))
        try:
            op = staged.edit(
                session,
                op_id,
                body.inputs,
                by=identity(user),
                reason=body.reason,
                severity=body.severity,
                version=body.version,
            )
            return card(session, op)
        except staged.ApprovalError as exc:
            raise _translate(exc) from exc

    return r


def corrections_router() -> APIRouter:
    r = APIRouter(prefix="/corrections", tags=["approvals"])

    @r.get("", response_model=list[CorrectionOut])
    def corrections(
        _: CurrentUser, session: DbSession, limit: Annotated[int, Query(ge=1, le=1000)] = 100
    ) -> list[Any]:
        """Approver edits, newest first: the draft, the edit, the reason and its severity."""
        return staged.corrections(session, limit)

    return r
