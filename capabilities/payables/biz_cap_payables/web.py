"""Owner-only bank change routes. Mounted under /v1 by the composition (never /webhooks).

1. POST /payables/bank_changes records a requested change and returns the number to call,
   taken from our records.
2. POST /payables/bank_changes/{id}/confirm, after the call-back, stages the confirmation
   in the approval queue (money action: always the owner's approval).
3. POST /payables/bank_changes/{id}/apply runs the approved confirmation, exactly once.
"""

from __future__ import annotations

import uuid
from typing import Any

from biz_kernel.approvals import staged
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth.deps import DbSession, OwnerUser
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from biz_cap_payables.models import BankChange
from biz_cap_payables.tools import _digits

CONFIRM = "payables.bank_change_confirm"
ACTOR = "payables"


class ChangeIn(BaseModel):
    supplier_party_id: uuid.UUID
    account: str
    account_name: str
    source: str


class ConfirmIn(BaseModel):
    called_number: str


def _op(session: Any, change_id: uuid.UUID) -> StagedOperation | None:
    return session.scalar(
        select(StagedOperation)
        .where(
            StagedOperation.tool == CONFIRM,
            StagedOperation.inputs["change_id"].astext == str(change_id),
            StagedOperation.status.in_(("pending", "approved", "executed")),
        )
        .order_by(StagedOperation.created_at.desc())
        .limit(1)
    )


def build_owner_router(rt: Any) -> APIRouter:
    router = APIRouter(prefix="/payables", tags=["payables"])

    @router.post("/bank_changes", status_code=201)
    def request_change(body: ChangeIn, user: OwnerUser, session: DbSession) -> dict[str, Any]:
        try:
            out = rt.registry.call(
                session,
                rt.policy,
                actor=ACTOR,
                name="payables.bank_change_request",
                args={**body.model_dump(mode="json"), "source": f"{body.source} (by {user.email})"},
            ).output
        except (LookupError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return dict(out)

    @router.post("/bank_changes/{change_id}/confirm", status_code=202)
    def confirm(
        change_id: uuid.UUID, body: ConfirmIn, user: OwnerUser, session: DbSession
    ) -> dict[str, Any]:
        existing = _op(session, change_id)
        if existing is not None:
            return {"op_id": str(existing.id), "status": existing.status}
        change = session.get(BankChange, change_id)
        if change is None:
            raise HTTPException(404, "no such bank change")
        if not change.callback_phone or _digits(body.called_number) != _digits(
            change.callback_phone
        ):  # checked again when it runs; this just fails fast
            raise HTTPException(422, "the number called is not the supplier's number on record")
        args = {
            "change_id": str(change_id),
            "called_number": body.called_number,
            "verified_by": str(user.email),
            "idempotency_key": f"bank_change:{change_id}",
        }
        res = staged.run_or_stage(session, rt.registry, rt.policy, ACTOR, CONFIRM, args)
        if not isinstance(res, staged.StagedOperation):  # policy must always ask the owner
            raise HTTPException(500, "bank change confirmation was not sent for approval")
        return {"op_id": str(res.id), "status": res.status}

    @router.post("/bank_changes/{change_id}/apply")
    def apply(change_id: uuid.UUID, user: OwnerUser, session: DbSession) -> dict[str, Any]:
        op = _op(session, change_id)
        if op is None:
            raise HTTPException(404, "no confirmation for this change")
        if op.status == "executed":
            return {"status": "applied", "replayed": True}
        try:
            result = staged.execute(session, op.id, rt.registry, rt.policy)
        except staged.NotApproved as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if not result.ran:
            raise HTTPException(403, getattr(result.decision, "reason", "blocked"))
        return dict(result.output)

    return router
