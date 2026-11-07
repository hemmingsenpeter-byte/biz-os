"""Autonomy ladder: 1 remember/draft, 2 recommend, 3 act and report, 4 silent."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.approvals.models import AutonomyLevel
from biz_kernel.audit.log import record
from biz_kernel.db import utcnow

MIN_LEVEL, MAX_LEVEL = 1, 4
DEFAULT_STREAK_BAR = 20


def _row(session: Session, role: str, decision_type: str) -> AutonomyLevel:
    """The counters row. A new row has no level: counting must never pin one."""
    row = session.scalar(
        select(AutonomyLevel).where(
            AutonomyLevel.role == role, AutonomyLevel.decision_type == decision_type
        )
    )
    if row is None:
        row = AutonomyLevel(
            role=role,
            decision_type=decision_type,
            level=None,
            approved_unchanged_streak=0,
            good_outcomes=0,
            bad_outcomes=0,
        )
        session.add(row)
        session.flush()
    return row


def level(session: Session, role: str, decision_type: str, default: int = 1) -> int:
    """Current level; `default` applies when nothing is stored (and is not stored)."""
    row = session.scalar(
        select(AutonomyLevel).where(
            AutonomyLevel.role == role, AutonomyLevel.decision_type == decision_type
        )
    )
    return row.level if row is not None and row.level is not None else default


def set_level(session: Session, role: str, decision_type: str, new: int, by: str) -> int:
    """Explicit owner set (also used for seeding)."""
    if not MIN_LEVEL <= new <= MAX_LEVEL:
        raise ValueError("level must be 1-4")
    row = _row(session, role, decision_type)
    old, row.level, row.updated_at = row.level, new, utcnow()
    session.flush()
    record(session, by, "autonomy", f"{role}/{decision_type}", old=old, new=new, how="set")
    return new


def record_approval(session: Session, role: str, decision_type: str, unchanged: bool) -> None:
    """Count an approval: unchanged ones extend the streak, edited ones reset it."""
    row = _row(session, role, decision_type)
    row.approved_unchanged_streak = row.approved_unchanged_streak + 1 if unchanged else 0
    session.flush()


def record_outcome(
    session: Session, role: str, decision_type: str, good: bool, default: int = 1
) -> int:
    """Any bad outcome demotes one level (min 1) and resets the streak.

    `default` is the configured level, used when no level has been stored yet."""
    row = _row(session, role, decision_type)
    if good:
        row.good_outcomes += 1
    else:
        old = row.level if row.level is not None else default
        row.bad_outcomes += 1
        row.level = max(MIN_LEVEL, old - 1)
        row.approved_unchanged_streak = 0
        record(
            session,
            role,
            "autonomy",
            f"{role}/{decision_type}",
            old=old,
            new=row.level,
            how="demote",
        )
    row.updated_at = utcnow()
    session.flush()
    return row.level if row.level is not None else default


def promotion_due(
    session: Session,
    role: str,
    decision_type: str,
    bar: int = DEFAULT_STREAK_BAR,
    default: int = 1,
) -> bool:
    row = _row(session, role, decision_type)
    current = row.level if row.level is not None else default
    return current < MAX_LEVEL and row.approved_unchanged_streak >= bar


def promote(session: Session, role: str, decision_type: str, by: str, default: int = 1) -> int:
    """Raise one level from the stored (else configured) one. Only ever an owner decision."""
    row = _row(session, role, decision_type)
    old = row.level if row.level is not None else default
    row.level = min(MAX_LEVEL, old + 1)
    row.approved_unchanged_streak = 0
    row.updated_at = utcnow()
    session.flush()
    record(
        session, by, "autonomy", f"{role}/{decision_type}", old=old, new=row.level, how="promote"
    )
    return row.level
