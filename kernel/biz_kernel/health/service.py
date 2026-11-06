"""Register schedules, report runs, raise alerts for missed or failed runs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.health.models import Alert, Schedule


class Alerter(Protocol):
    """Delivers a new alert (text message, daily brief)."""

    def notify(self, alert: Alert) -> None: ...


def raise_alert(
    session: Session,
    kind: str,
    subject: str,
    message: str,
    now: datetime,
    alerter: Alerter | None = None,
) -> Alert | None:
    """Create an alert unless one with the same kind and subject already exists."""
    exists = session.scalar(select(Alert.id).where(Alert.kind == kind, Alert.subject == subject))
    if exists is not None:
        return None
    alert = Alert(kind=kind, subject=subject, message=message, at=now)
    session.add(alert)
    session.flush()
    if alerter is not None:
        alerter.notify(alert)
    return alert


def register_schedule(session: Session, name: str, every: timedelta, now: datetime) -> Schedule:
    """Idempotent: updates the cadence of an existing schedule."""
    sched = session.scalar(select(Schedule).where(Schedule.name == name))
    if sched is None:
        sched = Schedule(name=name, every_seconds=int(every.total_seconds()), registered_at=now)
        session.add(sched)
    else:
        sched.every_seconds = int(every.total_seconds())
    session.flush()
    return sched


def report_run(session: Session, name: str, ok: bool, now: datetime) -> Schedule:
    sched = session.scalar(select(Schedule).where(Schedule.name == name))
    if sched is None:
        raise KeyError(f"unknown schedule {name!r}")
    sched.last_run_at = now
    sched.last_ok = ok
    session.flush()
    return sched


def check_missed(
    session: Session,
    now: datetime,
    grace: timedelta | None = None,
    alerter: Alerter | None = None,
) -> list[Alert]:
    """Alert once per missed cycle, and once per failed run. Default grace is 10% of the cycle."""
    raised: list[Alert] = []
    for sched in session.scalars(select(Schedule).order_by(Schedule.name)):
        every = timedelta(seconds=sched.every_seconds)
        slack = grace if grace is not None else every / 10
        base = sched.last_run_at or sched.registered_at
        cycles = (now - base) // every if every else 0
        due = base + every * max(cycles, 1)
        alert: Alert | None = None
        if now > due + slack:
            alert = raise_alert(
                session,
                "schedule.missed",
                f"{sched.name}@{due.isoformat()}",
                f"{sched.name} should have run by {due.isoformat()} and has not",
                now,
                alerter,
            )
        elif sched.last_ok is False and sched.last_run_at is not None:
            alert = raise_alert(
                session,
                "schedule.failed",
                f"{sched.name}@{sched.last_run_at.isoformat()}",
                f"{sched.name} failed at {sched.last_run_at.isoformat()}",
                now,
                alerter,
            )
        if alert is not None:
            raised.append(alert)
    return raised


def open_alerts(session: Session) -> list[Alert]:
    return list(
        session.scalars(select(Alert).where(Alert.acknowledged.is_(False)).order_by(Alert.at))
    )
