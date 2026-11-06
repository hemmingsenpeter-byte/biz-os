from datetime import UTC, datetime

from biz_kernel.budgets import BudgetBook


def test_unmetered_role_and_month_boundary(session):
    book = BudgetBook({"a": 2})
    book.charge(session, "free", 1000, "tool", None, datetime(2026, 12, 31, tzinfo=UTC))
    assert book.can_spend(session, "free", datetime(2026, 12, 31, tzinfo=UTC))
    book.charge(session, "a", 2, "model", None, datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert book.status(session, "a", datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == "stopped"
    assert book.status(session, "a", datetime(2027, 1, 1, tzinfo=UTC)) == "ok"
