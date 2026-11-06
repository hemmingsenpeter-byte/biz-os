from datetime import UTC, datetime, timedelta
from decimal import Decimal

from biz_kernel.goals import add_reading, load_goals, scorecard

T0 = datetime(2026, 3, 1, tzinfo=UTC)
TREE = {
    "goals": [
        {
            "key": "mission",
            "target": 100,
            "owner_role": "owner",
            "unit": "pct",
            "children": [
                {"key": "leads", "target": 20, "owner_role": "sales", "unit": "count"},
                {"key": "response_hours", "target": 4, "direction": "lower", "unit": "h"},
            ],
        }
    ]
}


def test_load_tree_and_rag(session):
    goals = load_goals(session, TREE)
    by = {g.key: g for g in goals}
    assert by["leads"].parent_id == by["mission"].id
    load_goals(session, TREE)  # idempotent
    add_reading(session, "leads", 15, T0)
    add_reading(session, "leads", 19, T0 + timedelta(days=1))
    add_reading(session, "response_hours", 4.2, T0)
    add_reading(session, "response_hours", 6, T0 + timedelta(days=1))
    now = T0 + timedelta(days=2)
    lines = {line.key: line for line in scorecard(session, now)}
    assert lines["leads"].actual == Decimal("19") and lines["leads"].trend == "up"
    assert lines["leads"].rag == "amber"
    assert lines["response_hours"].rag == "red" and lines["response_hours"].trend == "up"
    assert lines["mission"].rag == "red" and lines["mission"].actual is None
    add_reading(session, "leads", 20, now)
    add_reading(session, "response_hours", 3, now)
    lines = {line.key: line for line in scorecard(session, now)}
    assert lines["leads"].rag == "green"
    assert lines["response_hours"].rag == "green" and lines["response_hours"].trend == "down"
