"""Suggestion routes. Staff view; only the owner approves or rejects."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from biz_kernel.api.routes_approvals import ApprovalOut
from biz_kernel.approvals import staged, suggestions
from biz_kernel.approvals.models import StagedOperation, Suggestion
from biz_kernel.auth.deps import CurrentUser, DbSession, OwnerUser


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    source: str
    title: str
    tool: str
    inputs: dict[str, Any]
    reasons: list[str]
    evidence: dict[str, Any]
    status: str
    staged_op_id: uuid.UUID | None


def router() -> APIRouter:
    r = APIRouter(prefix="/suggestions", tags=["suggestions"])

    @r.get("", response_model=list[SuggestionOut])
    def listing(
        _: CurrentUser, session: DbSession, status: str | None = "proposed"
    ) -> list[Suggestion]:
        q = select(Suggestion).order_by(Suggestion.created_at)
        if status:
            q = q.where(Suggestion.status == status)
        return list(session.scalars(q))

    @r.post("/{sid}/approve", response_model=ApprovalOut)
    def approve(
        sid: uuid.UUID, owner: OwnerUser, session: DbSession, request: Request
    ) -> StagedOperation:
        try:
            return suggestions.approve_suggestion(
                session, sid, staged.OWNER, getattr(request.app.state, "registry", None)
            )
        except staged.ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    @r.post("/{sid}/reject", response_model=SuggestionOut)
    def reject(sid: uuid.UUID, owner: OwnerUser, session: DbSession) -> Suggestion:
        try:
            return suggestions.reject_suggestion(session, sid, staged.OWNER)
        except staged.ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    return r
