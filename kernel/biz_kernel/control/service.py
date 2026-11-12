"""Pause and resume every role.

While paused: the heartbeat starts no role, running SOPs park before their next step, no
tool with an outside effect runs (not even an approved one), and nothing can be approved.
Inbound work is still collected, so nothing is lost; it runs after the resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.control.models import PauseEvent


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


def state(session: Session) -> PauseState:
    last = session.scalar(select(PauseEvent).order_by(PauseEvent.id.desc()).limit(1))
    if last is None:
        return PauseState(False)
    return PauseState(last.paused, last.by, last.reason, last.at)


def is_paused(session: Session) -> bool:
    return bool(session.scalar(select(PauseEvent.paused).order_by(PauseEvent.id.desc()).limit(1)))


def _set(session: Session, paused: bool, by: str, reason: str) -> PauseState:
    if is_paused(session) != paused:  # idempotent: a second pause keeps the first's reason
        session.add(PauseEvent(paused=paused, by=by, reason=reason))
        session.flush()
        record(session, by, "control", "pause" if paused else "resume", reason=reason)
    return state(session)


def pause(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, True, by, reason)


def resume(session: Session, by: str, reason: str = "") -> PauseState:
    return _set(session, False, by, reason)
