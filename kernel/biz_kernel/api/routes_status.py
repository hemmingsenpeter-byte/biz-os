"""Read-only status routes: goal scorecard, health alerts, tasks."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.db import utcnow
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.goals.service import scorecard
from biz_kernel.health.models import Alert
from biz_kernel.health.service import open_alerts
from biz_kernel.records.models import Task


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    target: Decimal
    actual: Decimal | None
    trend: str
    rag: str


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    subject: str
    message: str
    at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_id: uuid.UUID | None
    title: str
    kind: str
    goal_key: str | None
    due_at: datetime | None
    assignee: str | None
    status: str


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    ref: str | None
    task_id: uuid.UUID | None
    job_id: uuid.UUID | None
    at: datetime


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/goals/scorecard", response_model=list[ScoreOut], tags=["goals"])
    def goals_scorecard(_: CurrentUser, session: DbSession) -> list:  # type: ignore[type-arg]
        return scorecard(session, utcnow())

    @r.get("/health/alerts", response_model=list[AlertOut], tags=["health"])
    def health_alerts(_: CurrentUser, session: DbSession) -> list[Alert]:
        return open_alerts(session)

    @r.get("/tasks", response_model=list[TaskOut], tags=["tasks"])
    def tasks(_: CurrentUser, session: DbSession, status: str | None = None) -> list[Task]:
        q = select(Task).order_by(Task.due_at.nulls_last(), Task.id)
        if status:
            q = q.where(Task.status == status)
        return list(session.scalars(q))

    @r.get("/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
    def task(task_id: uuid.UUID, _: CurrentUser, session: DbSession) -> Task:
        row = session.get(Task, task_id)
        if row is None:
            raise HTTPException(404, "no such task")
        return row

    @r.get("/tasks/{task_id}/activity", response_model=list[ActivityOut], tags=["tasks"])
    def task_activity(
        task_id: uuid.UUID, _: CurrentUser, session: DbSession
    ) -> list[EvidenceEvent]:
        """Evidence that can support this task, including unassigned evidence on its job."""
        row = session.get(Task, task_id)
        if row is None:
            raise HTTPException(404, "no such task")
        scope = EvidenceEvent.task_id == task_id
        if row.job_id is not None:
            scope = or_(
                scope,
                EvidenceEvent.task_id.is_(None) & (EvidenceEvent.job_id == row.job_id),
            )
        return list(
            session.scalars(
                select(EvidenceEvent).where(scope).order_by(EvidenceEvent.at, EvidenceEvent.id)
            )
        )

    return r
