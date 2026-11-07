"""Record evidence; mark tasks done only when declared evidence exists.

A flush guard rejects any other write that sets a task to done.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event, or_, select, text
from sqlalchemy.orm import Session

from biz_kernel.db import utcnow
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.records.models import Task

EvidenceRules = dict[str, list[str]]  # task kind -> required evidence kinds


def merge_rules(*rule_sets: EvidenceRules) -> tuple[EvidenceRules, list[str]]:
    """Union per task kind (the strictest rule), plus the kinds declared differently."""
    merged: EvidenceRules = {}
    conflicts: set[str] = set()
    for rules in rule_sets:
        for kind, required in rules.items():
            if kind not in merged:
                merged[kind] = list(required)
                continue
            if set(merged[kind]) != set(required):
                conflicts.add(kind)
            merged[kind] += [k for k in required if k not in merged[kind]]
    return merged, sorted(conflicts)


_ALLOWED = "evidence_complete_ids"


class EvidenceMissing(Exception):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"missing evidence: {', '.join(missing)}")
        self.missing = missing


def note(
    session: Session,
    kind: str,
    ref: str | None = None,
    job_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> EvidenceEvent:
    ev = EvidenceEvent(kind=kind, ref=ref, job_id=job_id, task_id=task_id, at=utcnow())
    session.add(ev)
    session.flush()
    return ev


def _has(session: Session, task: Task, kind: str) -> bool:
    """Task-level evidence first; job-level evidence counts when it names no task."""
    cond = EvidenceEvent.task_id == task.id
    if task.job_id is not None:
        cond = or_(cond, (EvidenceEvent.task_id.is_(None)) & (EvidenceEvent.job_id == task.job_id))
    return (
        session.scalar(select(EvidenceEvent.id).where(EvidenceEvent.kind == kind, cond).limit(1))
        is not None
    )


def complete_task(session: Session, task_id: uuid.UUID, rules: EvidenceRules) -> Task:
    """Set status done only if every required evidence kind is present. Undeclared kinds fail."""
    task = session.get(Task, task_id)
    if task is None:
        raise KeyError(f"unknown task {task_id}")
    if task.kind not in rules:
        raise EvidenceMissing([f"<no evidence declared for task kind {task.kind!r}>"])
    missing = [k for k in rules[task.kind] if not _has(session, task, k)]
    if missing:
        raise EvidenceMissing(missing)
    allowed: set[uuid.UUID] = session.info.setdefault(_ALLOWED, set())
    allowed.add(task.id)
    session.execute(text("SELECT set_config('biz.task_done_ok', :id, true)"), {"id": str(task.id)})
    task.status = "done"
    try:
        session.flush()
    finally:
        allowed.discard(task.id)
        session.execute(text("SELECT set_config('biz.task_done_ok', '', true)"))
    return task


@event.listens_for(Session, "before_flush")
def _guard_done(session: Session, ctx: Any, instances: Any) -> None:
    allowed: set[uuid.UUID] = session.info.get(_ALLOWED, set())
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Task) and obj.status == "done" and obj.id not in allowed:
            hist = session.is_modified(obj, include_collections=False)
            if obj in session.new or hist:
                raise EvidenceMissing(["<use complete_task: done needs declared evidence>"])
