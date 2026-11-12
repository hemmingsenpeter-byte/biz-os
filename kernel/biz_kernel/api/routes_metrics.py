"""Read-only measurement report for the owner dashboard."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.metrics import MetricsReport, report, window
from biz_kernel.metrics.service import DEFAULT_DAYS, MAX_DAYS


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/metrics", response_model=MetricsReport, tags=["metrics"])
    def metrics(
        request: Request,
        _: CurrentUser,
        session: DbSession,
        days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
    ) -> MetricsReport:
        manifest = getattr(request.app.state, "manifest", None)
        policy = getattr(manifest, "policy", None) or {}
        since, until = window(days)
        return report(session, since, until, policy.get("metrics"))

    return r
