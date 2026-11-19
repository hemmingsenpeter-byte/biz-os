"""The kill switch. Anyone signed in may pause; only the owner may resume."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from biz_kernel.api.routes_approvals import identity
from biz_kernel.api.routes_shadow import current_mode
from biz_kernel.auth.deps import CurrentUser, DbSession, OwnerUser
from biz_kernel.control import service as control


class PauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    paused: bool
    by: str | None
    reason: str
    at: datetime | None
    mode: str = "live"  # live | shadow (policy.yaml); shadow: nothing outside is ever done


class PauseIn(BaseModel):
    reason: str = ""


def _out(state: control.PauseState, request: Request) -> PauseOut:
    return PauseOut(
        paused=state.paused,
        by=state.by,
        reason=state.reason,
        at=state.at,
        mode=current_mode(request),
    )


def router() -> APIRouter:
    r = APIRouter(prefix="/control", tags=["control"])

    @r.get("", response_model=PauseOut)
    def state(request: Request, _: CurrentUser, session: DbSession) -> PauseOut:
        return _out(control.state(session), request)

    @r.post("/pause", response_model=PauseOut)
    def pause(request: Request, body: PauseIn, user: CurrentUser, session: DbSession) -> PauseOut:
        return _out(control.pause(session, identity(user), body.reason), request)

    @r.post("/resume", response_model=PauseOut)
    def resume(request: Request, user: OwnerUser, session: DbSession) -> PauseOut:
        return _out(control.resume(session, identity(user)), request)

    return r
