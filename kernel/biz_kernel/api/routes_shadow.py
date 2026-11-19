"""Shadow (practice) mode report for the owner console."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.policy import shadow_report
from biz_kernel.policy.mode import mode_of


def current_mode(request: Request) -> str:
    manifest = getattr(request.app.state, "manifest", None)
    return mode_of(getattr(manifest, "policy", None))


def router() -> APIRouter:
    r = APIRouter(tags=["control"])

    @r.get("/shadow", response_model=shadow_report.ShadowReport)
    def shadow(
        request: Request,
        _: CurrentUser,
        session: DbSession,
        days: Annotated[int, Query(ge=1, le=shadow_report.MAX_DAYS)] = shadow_report.DEFAULT_DAYS,
    ) -> shadow_report.ShadowReport:
        return shadow_report.report(session, current_mode(request), days)

    return r
