"""Coalescing wake-up queue, cadence scheduling and the heartbeat run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import Table, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from biz_kernel.budgets.service import BudgetBook
from biz_kernel.db import uuid7
from biz_kernel.health.models import Schedule
from biz_kernel.health.service import register_schedule, report_run
from biz_kernel.heartbeats.models import Wakeup

log = logging.getLogger(__name__)

RoleRunner = Callable[[str, list[str]], None]
Sweep = Callable[[Session], object]


def enqueue(session: Session, role: str, reason: str, now: datetime) -> None:
    """Add a wake-up, or merge into the role's pending one (reasons joined, count + 1)."""
    t = cast(Table, Wakeup.__table__)
    stmt = insert(t).values(
        id=uuid7(),
        role=role,
        reasons=[reason],
        due_at=now,
        status="pending",
        coalesced_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["role"],
        index_where=text("status = 'pending'"),
        set_={
            "reasons": func.array_append(t.c.reasons, reason),
            "coalesced_count": t.c.coalesced_count + 1,
            "due_at": func.least(t.c.due_at, now),
        },
    )
    session.execute(stmt)
    session.flush()
    session.expire_all()


def claim_due(session: Session, now: datetime) -> list[Wakeup]:
    """Claim pending wake-ups that are due. SKIP LOCKED lets parallel workers share the queue."""
    rows = list(
        session.scalars(
            select(Wakeup)
            .where(Wakeup.status == "pending", Wakeup.due_at <= now)
            .order_by(Wakeup.due_at)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    for w in rows:
        w.status = "claimed"
    session.flush()
    return rows


def schedule_cadence(session: Session, roles: dict[str, timedelta], now: datetime) -> list[str]:
    """Enqueue every role whose cadence has elapsed since its last heartbeat run."""
    woken: list[str] = []
    for role, every in roles.items():
        sched = register_schedule(session, f"heartbeat:{role}", every, now)
        last = sched.last_run_at or sched.registered_at
        pending = session.scalar(
            select(Wakeup.id).where(Wakeup.role == role, Wakeup.status == "pending")
        )
        if now - last >= every and pending is None:
            enqueue(session, role, "cadence", now)
            woken.append(role)
    return woken


def _requeue(session: Session, w: Wakeup) -> None:
    """Budget-stopped: keep the work. Back to pending, or merged into a newer pending one."""
    newer = session.scalar(
        select(Wakeup).where(Wakeup.role == w.role, Wakeup.status == "pending", Wakeup.id != w.id)
    )
    if newer is None:
        w.status, w.note = "pending", "waiting: budget stopped"
        return
    newer.reasons = [*w.reasons, *newer.reasons]
    newer.coalesced_count += w.coalesced_count
    newer.due_at = min(newer.due_at, w.due_at)
    w.status, w.note = "done", "merged: budget stopped"


def run_heartbeat(
    session: Session,
    role_runner: RoleRunner,
    budgets: BudgetBook,
    sweep: Sweep,
    now: datetime,
) -> list[str]:
    """Run due wake-ups (budget-checked), then the follow-through sweep. Returns roles run."""
    ran: list[str] = []
    for w in claim_due(session, now):
        name = f"heartbeat:{w.role}"
        if session.scalar(select(Schedule.id).where(Schedule.name == name)) is None:
            register_schedule(session, name, timedelta(hours=1), now)
        if not budgets.can_spend(session, w.role, now):
            _requeue(session, w)
            continue
        try:
            role_runner(w.role, list(w.reasons))
            ok, w.note = True, None
        except Exception as exc:
            log.exception("heartbeat for %s failed", w.role)
            ok, w.note = False, f"error: {exc}"
        w.status = "done"
        report_run(session, name, ok, now)
        ran.append(w.role)
    session.flush()
    sweep(session)
    return ran
