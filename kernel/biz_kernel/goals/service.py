"""Load goals from parsed goals.yaml, record readings, build the scorecard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.goals.models import Goal, GoalReading


@dataclass(frozen=True)
class ScoreLine:
    key: str
    target: Decimal
    actual: Decimal | None
    trend: str  # up | down | flat | none
    rag: str  # green | amber | red


def load_goals(session: Session, data: dict[str, Any] | list[dict[str, Any]]) -> list[Goal]:
    """Upsert a goal tree by key. Accepts {"goals": [...]} or a list; children under "children"."""
    nodes = data["goals"] if isinstance(data, dict) else data
    loaded: list[Goal] = []

    def walk(node: dict[str, Any], parent: Goal | None) -> None:
        goal = session.scalar(select(Goal).where(Goal.key == node["key"]))
        if goal is None:
            goal = Goal(key=node["key"], target=Decimal(str(node["target"])))
            session.add(goal)
        direction = node.get("direction", "higher")
        if direction not in ("higher", "lower"):
            raise ValueError(f"goal {node['key']}: direction must be higher or lower")
        goal.target = Decimal(str(node["target"]))
        goal.owner_role = node.get("owner_role")
        goal.period = node.get("period", "month")
        goal.unit = node.get("unit", "")
        goal.direction = direction
        goal.parent_id = None
        session.flush()
        if parent is not None:
            goal.parent_id = parent.id
        loaded.append(goal)
        for child in node.get("children", []):
            walk(child, goal)

    for n in nodes:
        walk(n, None)
    session.flush()
    return loaded


def add_reading(
    session: Session,
    key: str,
    value: Decimal | float | int | str,
    now: datetime,
    source: str | None = None,
) -> GoalReading:
    goal = session.scalar(select(Goal).where(Goal.key == key))
    if goal is None:
        raise KeyError(f"unknown goal {key!r}")
    reading = GoalReading(goal_id=goal.id, value=Decimal(str(value)), at=now, source=source)
    session.add(reading)
    session.flush()
    return reading


def _rag(goal: Goal, actual: Decimal | None) -> str:
    if actual is None:
        return "red"
    t = goal.target
    if goal.direction == "lower":
        if actual <= t:
            return "green"
        return "amber" if actual <= t * Decimal("1.1") else "red"
    if actual >= t:
        return "green"
    return "amber" if actual >= t * Decimal("0.9") else "red"


def scorecard(session: Session, now: datetime) -> list[ScoreLine]:
    """Latest reading at or before now per goal; trend vs the reading before it."""
    lines: list[ScoreLine] = []
    for goal in session.scalars(select(Goal).order_by(Goal.key)):
        readings = list(
            session.scalars(
                select(GoalReading)
                .where(GoalReading.goal_id == goal.id, GoalReading.at <= now)
                .order_by(GoalReading.at.desc())
                .limit(2)
            )
        )
        actual = readings[0].value if readings else None
        if len(readings) < 2:
            trend = "none"
        else:
            diff = readings[0].value - readings[1].value
            trend = "up" if diff > 0 else "down" if diff < 0 else "flat"
        lines.append(ScoreLine(goal.key, goal.target, actual, trend, _rag(goal, actual)))
    return lines
