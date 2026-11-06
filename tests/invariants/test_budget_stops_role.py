"""A role at 100% of its monthly budget makes no further paid calls; owner warned at 80%."""

from datetime import UTC, datetime

import pytest
from biz_kernel.budgets import BudgetBook, BudgetExceeded, CostEvent
from biz_kernel.health import open_alerts
from sqlalchemy import func, select

T0 = datetime(2026, 3, 10, tzinfo=UTC)


def _count(session):
    return session.scalar(select(func.count()).select_from(CostEvent))


def test_budget_stops_role(session):
    book = BudgetBook({"sales": 10})
    book.charge(session, "sales", 7, "model", "a", T0)
    assert book.status(session, "sales", T0) == "ok"
    assert open_alerts(session) == []
    book.charge(session, "sales", 1, "tool", "b", T0)  # 80%
    assert book.status(session, "sales", T0) == "warn"
    book.charge(session, "sales", 0.5, "tool", "c", T0)  # still warn: no second alert
    warns = [a for a in open_alerts(session) if a.kind == "budget.warn"]
    assert len(warns) == 1
    book.charge(session, "sales", 1.5, "model", "d", T0)  # reaches 10
    assert book.status(session, "sales", T0) == "stopped"
    assert not book.can_spend(session, "sales", T0)
    before = _count(session)
    with pytest.raises(BudgetExceeded):
        book.charge(session, "sales", 0.01, "model", "e", T0)
    assert _count(session) == before  # nothing recorded
    assert book.can_spend(session, "ops", T0)  # a role without a limit is unaffected


def test_resume_by_raising_limit_or_new_month(session):
    book = BudgetBook({"sales": 1})
    book.charge(session, "sales", 1, "model", None, T0)
    assert not book.can_spend(session, "sales", T0)
    book.raise_limit("sales", 5)
    assert book.can_spend(session, "sales", T0)
    book.raise_limit("sales", 1)
    assert book.status(session, "sales", datetime(2026, 4, 1, tzinfo=UTC)) == "ok"
    assert book.spent(session, "sales", datetime(2026, 4, 1, tzinfo=UTC)) == 0
