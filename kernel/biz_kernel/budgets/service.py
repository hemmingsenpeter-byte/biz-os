"""Monthly per-role budgets: warn at 80%, stop at 100%, resume when the limit is raised."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biz_kernel.budgets.models import CostEvent
from biz_kernel.health.service import Alerter, raise_alert

WARN_AT = Decimal("0.8")


class BudgetExceeded(Exception):
    def __init__(self, role: str, spent: Decimal, limit: Decimal) -> None:
        super().__init__(f"role {role} is at its monthly budget ({spent} of {limit} USD)")
        self.role = role


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


class BudgetBook:
    """Limits per role in USD. A role with no limit is unmetered."""

    def __init__(self, limits: dict[str, Decimal | float | int | str]) -> None:
        self.limits: dict[str, Decimal] = {r: Decimal(str(v)) for r, v in limits.items()}

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

    def charge(
        self,
        session: Session,
        role: str,
        amount: Decimal | float | int | str,
        kind: str,
        ref: str | None,
        now: datetime,
        alerter: Alerter | None = None,
    ) -> CostEvent:
        """Record a cost. Raises BudgetExceeded, recording nothing, if the role is stopped."""
        if kind not in ("model", "tool"):
            raise ValueError("kind must be model or tool")
        if self.status(session, role, now) == "stopped":
            raise BudgetExceeded(role, self.spent(session, role, now), self.limits[role])
        event = CostEvent(role=role, amount_usd=Decimal(str(amount)), kind=kind, ref=ref, at=now)
        session.add(event)
        session.flush()
        if self.status(session, role, now) != "ok":
            self._warn(session, role, now, alerter)
        return event

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
