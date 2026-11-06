"""The /v1 REST API. Capabilities mount routers under /v1/<capability>."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth.deps import CurrentUser, DbSession, OwnerUser
from biz_kernel.auth.routes import router as auth_router
from biz_kernel.contracts.ports import MailPort
from biz_kernel.packets.bus import Packet, timeline


class PacketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    type: str
    schema_version: int
    job_id: uuid.UUID | None
    from_part: str
    to_parts: list[str]
    summary: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    open_questions: list[Any]
    approved_by: str | None
    created_at: datetime


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    actor: str
    kind: str
    subject: str | None
    detail: dict[str, Any]


def _v1() -> APIRouter:
    router = APIRouter()

    @router.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    router.include_router(auth_router)

    @router.get("/packets", response_model=list[PacketOut], tags=["packets"])
    def list_packets(
        job_id: uuid.UUID,
        _: CurrentUser,
        session: DbSession,
        limit: Annotated[int, Query(le=1000)] = 200,
    ) -> list[Packet]:
        return timeline(session, job_id)[:limit]

    @router.get("/jobs/{job_id}/timeline", response_model=list[PacketOut], tags=["jobs"])
    def job_timeline(
        job_id: uuid.UUID,
        _: CurrentUser,
        session: DbSession,
    ) -> list[Packet]:
        return timeline(session, job_id)

    @router.get("/audit", response_model=list[AuditOut], tags=["audit"])
    def audit(
        _: OwnerUser,
        session: DbSession,
        kind: str | None = None,
        limit: Annotated[int, Query(le=1000)] = 100,
    ) -> list[AuditEntry]:
        q = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
        if kind:
            q = q.where(AuditEntry.kind == kind)
        return list(session.scalars(q))

    # TODO(approvals): the approvals agent mounts GET /v1/approvals, POST
    # /v1/approvals/{id}/decide here via `mount_capability(app, "approvals", router)`
    # or by including its router in this function. Owner-only decisions use `owner_only`.
    return router


def mount_capability(app: FastAPI, name: str, router: APIRouter) -> None:
    """Expose a capability's routes under /v1/<name>."""
    app.include_router(router, prefix=f"/v1/{name}", tags=[name])


def create_app(manifest: Any | None = None, mail: MailPort | None = None) -> FastAPI:
    app = FastAPI(
        title="BIZ OS", version="1.0.0", openapi_url="/v1/openapi.json", docs_url="/v1/docs"
    )
    app.state.manifest = manifest
    app.state.mail = mail
    app.include_router(_v1(), prefix="/v1")
    return app
