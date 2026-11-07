"""Monthly per-role budgets: warn at 80%, stop at 100%, resume when the limit is raised.

Spend never passes the limit on a known cost: `charge` refuses any amount that would take
the role over it, and `preflight` refuses before the work starts. A per-role Postgres
advisory lock (held to the end of the transaction) serialises the check and the insert, so
two workers can't both pass the check and overshoot together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from biz_kernel.budgets.models import CostEvent
from biz_kernel.health.service import Alerter, raise_alert

WARN_AT = Decimal("0.8")


class BudgetExceeded(Exception):
    def __init__(
        self, role: str, spent: Decimal, limit: Decimal, needed: Decimal = Decimal(0)
    ) -> None:
        more = f"; {needed} more needed" if needed else ""
        super().__init__(f"role {role} is at its monthly budget ({spent} of {limit} USD{more})")
        self.role, self.spent, self.limit, self.needed = role, spent, limit, needed

    def __reduce__(self):  # type: ignore[no-untyped-def]
        # DBOS pickles step errors and re-raises them on replay.
        return (BudgetExceeded, (self.role, self.spent, self.limit, self.needed))


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def _amount(value: Decimal | float | int | str) -> Decimal:
    """A finite, non-negative cost; anything else is a ValueError (fail closed)."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"cost {value!r} is not a number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"cost {value!r} must be finite and not negative")
    return amount


class BudgetBook:
    """Limits per role in USD. A role with no limit is unmetered."""

    def __init__(self, limits: dict[str, Decimal | float | int | str]) -> None:
        self.limits: dict[str, Decimal] = {r: Decimal(str(v)) for r, v in limits.items()}

    def _lock(self, session: Session, role: str) -> None:
        """Serialise budget checks and charges for one role until the transaction ends."""
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"biz.budget:{role}"}
            )

    def spent(self, session: Session, role: str, now: datetime) -> Decimal:
        start, end = _month_bounds(now)
        total = session.scalar(
            select(func.coalesce(func.sum(CostEvent.amount_usd), 0)).where(
                CostEvent.role == role, CostEvent.at >= start, CostEvent.at < end
            )
        )
        return Decimal(total or 0)

    def status(self, session: Session, role: str, now: datetime) -> str:
        limit = self.limits.get(role)
        if limit is None:
            return "ok"
        spent = self.spent(session, role, now)
        if spent >= limit:
            return "stopped"
        return "warn" if spent >= limit * WARN_AT else "ok"

    def can_spend(self, session: Session, role: str, now: datetime) -> bool:
        return self.status(session, role, now) != "stopped"

    def can_afford(
        self, session: Session, role: str, amount: Decimal | float | int | str, now: datetime
    ) -> bool:
        """True if the role is not stopped and `amount` more stays within the limit."""
        limit = self.limits.get(role)
        if limit is None:
            return True
        spent = self.spent(session, role, now)
        return spent < limit and spent + _amount(amount) <= limit

    def preflight(
        self, session: Session, role: str, amount: Decimal | float | int | str, now: datetime
    ) -> None:
        """Raise BudgetExceeded unless `amount` more stays within the role's limit."""
        cost = _amount(amount)
        if role not in self.limits:
            return
        self._lock(session, role)
        if not self.can_afford(session, role, cost, now):
            raise BudgetExceeded(role, self.spent(session, role, now), self.limits[role], cost)

    def estimate(self, session: Session, role: str, ref: str, now: datetime) -> Decimal:
        """Estimated cost of the next call for `ref`: the largest one this month (0 if none)."""
        start, end = _month_bounds(now)
        top = session.scalar(
            select(func.max(CostEvent.amount_usd)).where(
                CostEvent.role == role,
                CostEvent.ref == ref,
                CostEvent.at >= start,
                CostEvent.at < end,
            )
        )
        return Decimal(top or 0)

    def charge(
        self,
        session: Session,
        role: str,
        amount: Decimal | float | int | str,
        kind: str,
        ref: str | None,
        now: datetime,
        alerter: Alerter | None = None,
        *,
        incurred: bool = False,
    ) -> CostEvent:
        """Record a cost.

        Raises BudgetExceeded, recording nothing, if the role is stopped or the amount would
        take it over its limit. `incurred=True` is for money already spent (a model call that
        has returned): it is always recorded, so the ledger never hides real spend.
        """
        if kind not in ("model", "tool"):
            raise ValueError("kind must be model or tool")
        cost = _amount(amount)
        if role in self.limits:
            self._lock(session, role)
            if not incurred and not self.can_afford(session, role, cost, now):
                raise BudgetExceeded(role, self.spent(session, role, now), self.limits[role], cost)
        event = CostEvent(role=role, amount_usd=cost, kind=kind, ref=ref, at=now)
        session.add(event)
        session.flush()
        if self.status(session, role, now) != "ok":
            self._warn(session, role, now, alerter)
        return event

    def refund(self, session: Session, event: CostEvent) -> None:
        """Remove a charge made for work that then failed (same transaction only)."""
        session.delete(event)
        session.flush()

    def _warn(self, session: Session, role: str, now: datetime, alerter: Alerter | None) -> None:
        spent, limit = self.spent(session, role, now), self.limits[role]
        raise_alert(
            session,
            "budget.warn",
            f"{role}:{now.astimezone(UTC):%Y-%m}",
            f"{role} has used {spent} of {limit} USD this month",
            now,
            alerter,
        )

    def raise_limit(self, role: str, new: Decimal | float | int | str) -> None:
        """Owner raises the limit; a stopped role resumes if spend is now under it."""
        self.limits[role] = Decimal(str(new))
