"""Pause and resume every role.

While paused: the heartbeat starts no role, running SOPs park before their next step, no
tool with an outside effect runs (not even an approved one), and nothing can be approved.
Inbound work is still collected, so nothing is lost; it runs after the resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.control.models import PauseEvent

# One advisory lock key. Pause and resume take it exclusively. An approval or a dispatch
# takes it shared, without waiting (admit), and holds it to commit. So neither can
# interleave with a pause, and a pause never waits behind work that is itself waiting.
CONTROL_LOCK = 0x42495A434F4E5452

_TICK = text("interval '1 microsecond'")

# Newest event first. `at` comes from the database clock under the exclusive lock and is
# strictly increasing, so a writer's own clock (uuid7 ids) never decides the order.
_NEWEST = (PauseEvent.at.desc(), PauseEvent.id.desc())


class Paused(RuntimeError):
    """Raised instead of running an outside-effect tool while the system is paused."""

    def __init__(self, what: str = "") -> None:
        self.what = what
        super().__init__(f"paused: {what}" if what else "paused")

    def __reduce__(self) -> tuple[type[Paused], tuple[str]]:
        return (Paused, (self.what,))


@dataclass(frozen=True)
class PauseState:
    paused: bool
    by: str | None = None
    reason: str = ""
    at: datetime | None = None


def _latest(session: Session) -> PauseEvent | None:
    return session.scalar(select(PauseEvent).order_by(*_NEWEST).limit(1))


def state(session: Session) -> PauseState:
    last = _latest(session)
    if last is None:
        return PauseState(False)
    return PauseState(last.paused, last.by, last.reason, last.at)


def is_paused(session: Session) -> bool:
    return bool(session.scalar(select(PauseEvent.paused).order_by(*_NEWEST).limit(1)))


def admit(session: Session, what: str = "") -> None:
    """For approvals and dispatch: hold admission until commit; never waits.

    The caller may already hold a budget or operation lock, so it must not wait here. If
    a pause or resume is in progress it raises Paused, which must unwind the whole attempt
    transaction before a workflow retries.
    """
    admitted = session.scalar(
        text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": CONTROL_LOCK}
    )
    if not admitted:
        raise Paused("control transition in progress")
    if is_paused(session):
        raise Paused(what)


def _set(session: Session, paused: bool, by: str, reason: str) -> PauseState:
    # Exclusive: waits for approvals and dispatches in flight, and orders the events.
    session.execute(select(func.pg_advisory_xact_lock(CONTROL_LOCK)))
    if is_paused(session) != paused:  # idempotent: a second pause keeps the first's reason
        last = select(func.max(PauseEvent.at)).scalar_subquery()
        at = func.greatest(
            func.clock_timestamp(), func.coalesce(last, func.clock_timestamp()) + _TICK
        )
        session.add(PauseEvent(paused=paused, by=by, reason=reason, at=at))
        session.flush()
        record(session, by, "control", "pause" if paused else "resume", reason=reason)
    return state(session)


def pause(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, True, by, reason)


def resume(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, False, by, reason)
