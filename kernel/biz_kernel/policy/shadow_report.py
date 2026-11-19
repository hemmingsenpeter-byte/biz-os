"""Shadow report: what the roles would have done, and proof that nothing was done.

Read-only. `proposals` are the outside actions roles proposed while in shadow mode
(audit kind `shadow`); `external_actions` counts outside-effect tool calls that actually
ran in the window (audit kind `tool_call` with `outside`). The Stage 1 gate needs the
latter to be 0 for the whole shadow period.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biz_kernel.audit.log import AuditEntry
from biz_kernel.db import utcnow

DEFAULT_DAYS, MAX_DAYS, MAX_PROPOSALS = 14, 366, 500


class ShadowProposal(BaseModel):
    id: str
    at: datetime
    actor: str
    tool: str
    inputs: dict[str, Any]
    run_id: str | None
    task_id: str | None


class ShadowReport(BaseModel):
    mode: str
    since: datetime
    until: datetime
    external_actions: int
    proposals: list[ShadowProposal]


def report(session: Session, mode: str, days: int = DEFAULT_DAYS) -> ShadowReport:
    until = utcnow()
    since = until - timedelta(days=days)
    window = (AuditEntry.at >= since, AuditEntry.at < until)
    executed = session.scalar(
        select(func.count())
        .select_from(AuditEntry)
        .where(
            *window,
            AuditEntry.kind == "tool_call",
            AuditEntry.detail["outside"].astext == "true",
        )
    )
    rows = session.scalars(
        select(AuditEntry)
        .where(*window, AuditEntry.kind == "shadow")
        .order_by(AuditEntry.at.desc(), AuditEntry.id.desc())
        .limit(MAX_PROPOSALS)
    )
    proposals = [
        ShadowProposal(
            id=str(e.id),
            at=e.at,
            actor=e.actor,
            tool=e.subject or "",
            inputs=dict((e.detail or {}).get("inputs") or {}),
            run_id=(e.detail or {}).get("run_id"),
            task_id=(e.detail or {}).get("task_id"),
        )
        for e in rows
    ]
    return ShadowReport(
        mode=mode,
        since=since,
        until=until,
        external_actions=int(executed or 0),
        proposals=proposals,
    )


def render(rep: ShadowReport) -> str:
    lines = [
        f"Mode: {rep.mode}",
        f"Window: {rep.since:%Y-%m-%d %H:%M} to {rep.until:%Y-%m-%d %H:%M} UTC",
        f"Outside actions taken: {rep.external_actions}",
        f"Proposals recorded (would have been done): {len(rep.proposals)}",
    ]
    for p in rep.proposals[:20]:
        lines.append(f"  {p.at:%Y-%m-%d %H:%M}  {p.actor}  {p.tool}")
    return "\n".join(lines)
