"""Daily running totals for money limits.

The total is business-wide (every role's money actions count), per local calendar day in
`limits.day_timezone` (default UTC). A Postgres advisory lock held to the end of the
transaction serialises the check in policy.check with the insert after the tool runs, so
two workers can't both pass the check and overshoot the day together.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from biz_kernel.policy.models import MoneySpend

LOCK_KEY = "biz.money_day"


def lock(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": LOCK_KEY})


def day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def spent_today(
    session: Session,
    now: datetime,
    tz: str,
    *,
    tool: str | None = None,
    decision_type: str | None = None,
) -> Decimal:
    """Today's total, narrowed to one tool or decision type when the limit is keyed by one."""
    start, end = day_bounds(now, tz)
    q = select(func.coalesce(func.sum(MoneySpend.amount), 0)).where(
        MoneySpend.at >= start, MoneySpend.at < end
    )
    if tool is not None:
        q = q.where(MoneySpend.tool == tool)
    if decision_type is not None:
        q = q.where(MoneySpend.decision_type == decision_type)
    return Decimal(session.scalar(q) or 0)


def add(
    session: Session,
    *,
    actor: str,
    tool: str,
    decision_type: str | None,
    amount: Decimal,
    now: datetime,
) -> None:
    lock(session)
    session.add(
        MoneySpend(actor=actor, tool=tool, decision_type=decision_type, amount=amount, at=now)
    )
    session.flush()
