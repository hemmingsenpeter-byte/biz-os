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

_TICK = text("interval '1 microsecond'")


class Paused(RuntimeError):
    """Raised instead of running an outside-effect tool while the system is paused."""

    def __init__(self, what: str = "") -> None:
        super().__init__(f"paused: {what}" if what else "paused")


@dataclass(frozen=True)
class PauseState:
    paused: bool
    by: str | None = None
    reason: str = ""
    at: datetime | None = None


# One advisory lock key. Pause and resume take it exclusively; an approval takes it shared
# for the rest of its transaction, so a pause and an approval can't interleave.
LOCK_KEY = 0x6B696C6C  # "kill"

# Newest event first. `at` comes from the database clock under the exclusive lock and is
# strictly increasing, so a writer's own clock (uuid7 ids) never decides the order.
_NEWEST = (PauseEvent.at.desc(), PauseEvent.id.desc())


def state(session: Session) -> PauseState:
    last = session.scalar(select(PauseEvent).order_by(*_NEWEST).limit(1))
    if last is None:
        return PauseState(False)
    return PauseState(last.paused, last.by, last.reason, last.at)


def is_paused(session: Session) -> bool:
    return bool(session.scalar(select(PauseEvent.paused).order_by(*_NEWEST).limit(1)))


def guard(session: Session, what: str = "") -> None:
    """Raise Paused if paused; otherwise hold off any pause until this transaction ends.

    Call it first in a transaction that approves something, before it writes anything
    else, so lock order stays the same everywhere (this lock, then any others).
    """
    session.execute(select(func.pg_advisory_xact_lock_shared(LOCK_KEY)))
    if is_paused(session):
        raise Paused(what)


def _set(session: Session, paused: bool, by: str, reason: str) -> PauseState:
    # Exclusive: waits for approvals in flight, and orders pause and resume events.
    session.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))
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
