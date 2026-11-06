"""Approval queue routes. Staff view; only the owner or the named approver decides."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.auth.models import User

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


class ApproveIn(BaseModel):
    version: int


class RejectIn(BaseModel):
    reason: str = ""


class EditIn(BaseModel):
    inputs: dict[str, Any]


def identity(user: User) -> str:
    """The name a user decides under: the owner is `owner`, anyone else their email."""
    return staged.OWNER if user.role == "owner" else user.email


def _load(session: Session, op_id: uuid.UUID) -> StagedOperation:
    op = session.get(StagedOperation, op_id)
    if op is None:
        raise HTTPException(404, "no such approval")
    return op


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
    def queue(_: CurrentUser, session: DbSession) -> list[StagedOperation]:
        return staged.pending(session)

    @r.post("/{op_id}/approve", response_model=ApprovalOut)
    def approve(
        op_id: uuid.UUID, body: ApproveIn, user: CurrentUser, session: DbSession, request: Request
    ) -> StagedOperation:
        op = _load(session, op_id)
        _may_decide(user, op)
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
        return _load(session, op_id)

    @r.post("/{op_id}/reject", response_model=ApprovalOut)
    def reject(
        op_id: uuid.UUID, body: RejectIn, user: CurrentUser, session: DbSession, request: Request
    ) -> StagedOperation:
        op = _load(session, op_id)
        _may_decide(user, op)
        decide: Decide = request.app.state.decide
        try:
            decide(op_id, identity(user), approve=False, reason=body.reason)
        except (staged.ApprovalError, LookupError) as exc:
            raise _translate(exc) from exc
        session.expire_all()
        return _load(session, op_id)

    @r.post("/{op_id}/edit", response_model=ApprovalOut)
    def edit(
        op_id: uuid.UUID, body: EditIn, user: CurrentUser, session: DbSession
    ) -> StagedOperation:
        _may_decide(user, _load(session, op_id))
        try:
            return staged.edit(session, op_id, body.inputs, by=identity(user))
        except staged.ApprovalError as exc:
            raise _translate(exc) from exc

    return r
