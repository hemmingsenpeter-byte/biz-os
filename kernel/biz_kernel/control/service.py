"""Pause and resume every role.

While paused: the heartbeat starts no role, running SOPs park before their next step, no
tool with an outside effect runs (not even an approved one), and nothing can be approved.
Inbound work is still collected, so nothing is lost; it runs after the resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.control.models import PauseEvent
from biz_kernel.db import utcnow

CONTROL_LOCK = 0x42495A434F4E5452


class Paused(RuntimeError):
    """Raised instead of running an outside-effect tool while the system is paused."""

    def __init__(self, what: str = "") -> None:
        self.what = what
        super().__init__(f"paused: {what}" if what else "paused")

    def __reduce__(self) -> tuple:
        return (Paused, (self.what,))


@dataclass(frozen=True)
class PauseState:
    paused: bool
    by: str | None = None
    reason: str = ""
    at: datetime | None = None


def state(session: Session) -> PauseState:
    last = _latest(session)
    if last is None:
        return PauseState(False)
    return PauseState(last.paused, last.by, last.reason, last.at)


def is_paused(session: Session) -> bool:
    return state(session).paused


def _latest(session: Session) -> PauseEvent | None:
    return session.scalar(
        select(PauseEvent).order_by(PauseEvent.at.desc(), PauseEvent.id.desc()).limit(1)
    )


def admit(session: Session, what: str = "") -> None:
    """Hold dispatch admission until commit; transitions take the exclusive lock.

    Do not wait for that lock while holding a budget or operation lock. A Paused
    exception must unwind the whole attempt transaction before a workflow retries.
    """
    if session.get_bind().dialect.name == "postgresql":
        admitted = session.scalar(
            text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": CONTROL_LOCK}
        )
        if not admitted:
            raise Paused("control transition in progress")
    if is_paused(session):
        raise Paused(what)


def _set(session: Session, paused: bool, by: str, reason: str) -> PauseState:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CONTROL_LOCK})
    last = _latest(session)
    if (last.paused if last is not None else False) != paused:
        at = utcnow()
        if last is not None:
            previous = last.at if last.at.tzinfo else last.at.replace(tzinfo=UTC)
            at = max(at, previous + timedelta(microseconds=1))
        session.add(PauseEvent(paused=paused, by=by, reason=reason, at=at))
        session.flush()
        record(session, by, "control", "pause" if paused else "resume", reason=reason)
    return state(session)


def pause(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, True, by, reason)


def resume(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, False, by, reason)
