"""The kill switch. Anyone signed in may pause; only the owner may resume."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from biz_kernel.api.routes_approvals import identity
from biz_kernel.auth.deps import CurrentUser, DbSession, OwnerUser
from biz_kernel.control import service as control


class PauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    paused: bool
    by: str | None
    reason: str
    at: datetime | None


class PauseIn(BaseModel):
    reason: str = ""


def router() -> APIRouter:
    r = APIRouter(prefix="/control", tags=["control"])

    @r.get("", response_model=PauseOut)
    def state(_: CurrentUser, session: DbSession) -> control.PauseState:
        return control.state(session)

    @r.post("/pause", response_model=PauseOut)
    def pause(body: PauseIn, user: CurrentUser, session: DbSession) -> control.PauseState:
        return control.pause(session, identity(user), body.reason)

    @r.post("/resume", response_model=PauseOut)
    def resume(user: OwnerUser, session: DbSession) -> control.PauseState:
        return control.resume(session, identity(user))

    return r
