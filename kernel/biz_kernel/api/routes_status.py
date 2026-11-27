"""Read-only status routes: goal scorecard, health alerts, tasks."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.db import utcnow
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.goals.service import scorecard
from biz_kernel.health.models import Alert
from biz_kernel.health.service import open_alerts
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Task
from biz_kernel.tools.shadow import SHADOW_EVIDENCE


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


class VerdictOut(BaseModel):
    decision: str
    reason: str


class ShadowOut(BaseModel):
    """A suppression's policy evidence, without message bodies or recipients."""

    tool: str
    effects: list[str]
    policy: VerdictOut | None
    pre_approved: bool
    staged_op_id: str | None


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    ref: str | None
    task_id: uuid.UUID | None
    job_id: uuid.UUID | None
    at: datetime
    subject: str | None = None
    reason: str | None = None
    shadow: ShadowOut | None = None


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _shadow(entry: AuditEntry) -> ShadowOut:
    detail: dict[str, Any] = entry.detail or {}
    policy = detail.get("policy")
    verdict = (
        VerdictOut(decision=str(policy["decision"]), reason=str(policy.get("reason", "")))
        if isinstance(policy, dict) and isinstance(policy.get("decision"), str)
        else None
    )
    effects = detail.get("effects")
    staged_op_id = detail.get("staged_op_id")
    return ShadowOut(
        tool=entry.subject or "",
        effects=[str(effect) for effect in effects] if isinstance(effects, list) else [],
        policy=verdict,
        pre_approved=detail.get("pre_approved") is True,
        staged_op_id=staged_op_id if isinstance(staged_op_id, str) else None,
    )


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
    def task_activity(task_id: uuid.UUID, _: CurrentUser, session: DbSession) -> list[ActivityOut]:
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
        events = list(
            session.scalars(
                select(EvidenceEvent).where(scope).order_by(EvidenceEvent.at, EvidenceEvent.id)
            )
        )
        identifiers = {
            packet_id
            for event in events
            if (
                event.kind == "packet:escalation.raised"
                and (packet_id := _uuid(event.ref)) is not None
            )
        }
        escalations = (
            {
                str(packet.id): packet
                for packet in session.scalars(
                    select(Packet).where(
                        Packet.id.in_(identifiers), Packet.type == "escalation.raised"
                    )
                )
            }
            if identifiers
            else {}
        )
        audit_ids = {
            audit_id
            for event in events
            if event.kind == SHADOW_EVIDENCE and (audit_id := _uuid(event.ref)) is not None
        }
        suppressions = (
            {
                str(entry.id): _shadow(entry)
                for entry in session.scalars(
                    select(AuditEntry).where(
                        AuditEntry.id.in_(audit_ids), AuditEntry.kind == SHADOW_EVIDENCE
                    )
                )
            }
            if audit_ids
            else {}
        )
        activity = []
        for event in events:
            item = ActivityOut.model_validate(event)
            reference = _uuid(event.ref)
            packet = escalations.get(str(reference))
            if (
                event.kind == "packet:escalation.raised"
                and packet is not None
                and packet.job_id == event.job_id
            ):
                item.subject = packet.payload.get("subject")
                item.reason = packet.payload.get("reason")
            if event.kind == SHADOW_EVIDENCE and reference is not None:
                item.shadow = suppressions.get(str(reference))
            activity.append(item)
        return activity

    return r
