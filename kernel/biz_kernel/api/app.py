"""The /v1 REST API. Capabilities mount routers under /v1/<capability>."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from starlette.responses import JSONResponse, Response

from biz_kernel.api import routes_approvals, routes_control, routes_status, routes_suggestions
from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth import config
from biz_kernel.auth.deps import CurrentUser, DbSession, OwnerUser
from biz_kernel.auth.routes import router as auth_router
from biz_kernel.contracts.ports import MailPort
from biz_kernel.control.service import Paused
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

    router.include_router(routes_approvals.router())
    router.include_router(routes_approvals.corrections_router())
    router.include_router(routes_control.router())
    router.include_router(routes_suggestions.router())
    router.include_router(routes_status.router())
    return router


def _default_decide(*args: Any, **kwargs: Any) -> Any:
    """Approve or reject via the gate, which wakes the waiting SOP run."""
    from biz_kernel.workflows.gate import decide

    return decide(*args, **kwargs)


def mount_capability(app: FastAPI, name: str, router: APIRouter) -> None:
    """Expose a capability's routes under /v1/<name>."""
    app.include_router(router, prefix=f"/v1/{name}", tags=[name])


def create_app(
    manifest: Any | None = None,
    mail: MailPort | None = None,
    decide: Callable[..., Any] | None = None,
    registry: Any | None = None,
) -> FastAPI:
    app = FastAPI(
        title="BIZ OS", version="1.0.0", openapi_url="/v1/openapi.json", docs_url="/v1/docs"
    )

    @app.middleware("http")
    async def protect_browser_mutations(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The API can also be reached directly through Caddy. Enforce the browser
        # boundary here as well as in the web proxy; SameSite alone permits sibling
        # origins. Headerless API clients keep their existing authentication path.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/v1/"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin != config.origin().rstrip("/")) or site in {
                "same-site",
                "cross-site",
            }:
                return JSONResponse(
                    {"detail": "This action must be requested from the workspace origin."},
                    status_code=403,
                )
        return await call_next(request)

    app.state.manifest = manifest
    app.state.mail = mail
    app.state.registry = registry
    app.state.decide = decide or _default_decide
    app.include_router(_v1(), prefix="/v1")

    @app.exception_handler(Paused)
    def paused(_: Request, exc: Paused) -> JSONResponse:
        """Any route that would act while the kill switch is on answers 409."""
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app
