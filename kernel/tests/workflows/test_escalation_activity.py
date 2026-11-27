"""Generic workflow escalations produce durable owner evidence once, even on retry."""

from __future__ import annotations

import inspect
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from biz_kernel import db
from biz_kernel.audit.log import AuditEntry
from biz_kernel.evidence import service as evidence
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Bus, Packet, SchemaRegistry
from biz_kernel.records.models import Job, Task
from biz_kernel.workflows import runner
from biz_kernel.workflows.escalation import check, parse
from biz_kernel.workflows.models import SopRun
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "conditions,kind,output",
    [
        ([{"confidence_below": 0.6}], "draft", {"confidence": 0.4}),
        (["unpriced_item"], "tool", {"signals": ["unpriced_item"]}),
    ],
    ids=["confidence", "tool-signal"],
)
def test_generic_escalation_links_its_reason_once_after_uncheckpointed_retry(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    conditions: list[Any],
    kind: str,
    output: dict[str, Any],
) -> None:
    reason = check(parse(conditions, known_signals={"unpriced_item"}), kind=kind, output=output)
    assert reason is not None
    schemas = SchemaRegistry()
    schemas.load_dir(ROOT / "packets")
    bus = Bus(schemas)
    job = Job(kind="work", stage="open")
    session.add(job)
    session.flush()
    task = Task(title="Classify an uncertain request", assignee="fixture", job_id=job.id)
    session.add(task)
    session.flush()
    run = SopRun(
        id="generic-escalation", role="fixture", sop="classify", task_id=task.id, job_id=job.id
    )
    session.add(run)
    session.flush()

    @contextmanager
    def scope():  # type: ignore[no-untyped-def]
        yield session

    monkeypatch.setattr(runner.db, "session_scope", scope)
    monkeypatch.setattr(
        runner,
        "ctx",
        lambda: SimpleNamespace(bus=bus, evidence=SimpleNamespace(note=evidence.note)),
    )
    trigger = {"id": str(uuid.uuid4()), "summary": "Request needing review"}
    # The first database transaction may commit before DBOS checkpoints the step.
    # Repeating the actual step body must preserve one packet, link and audit entry.
    raise_escalation = inspect.unwrap(runner._raise_escalation)
    raise_escalation(run.id, run.role, trigger, reason)
    raise_escalation(run.id, run.role, trigger, reason)
    packet = session.scalars(select(Packet).where(Packet.type == "escalation.raised")).one()
    activity = session.scalars(select(EvidenceEvent).where(EvidenceEvent.task_id == task.id)).one()
    assert packet.payload["packet_id"] == trigger["id"]
    assert packet.payload["reason"] == reason
    assert activity.kind == "packet:escalation.raised"
    assert activity.ref == str(packet.id) and activity.job_id == job.id
    recorded_task = session.get(Task, task.id)
    assert recorded_task is not None and recorded_task.status == "open"
    audit = session.scalars(select(AuditEntry).where(AuditEntry.kind == "sop.escalate_when")).one()
    assert audit.detail["run_id"] == run.id and audit.detail["reason"] == reason


def test_concurrent_escalation_retries_keep_one_owner_activity(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_all_models()
    engine = create_engine(fresh_db)
    db.Base.metadata.create_all(engine)
    schemas = SchemaRegistry()
    schemas.load_dir(ROOT / "packets")
    with Session(engine) as session:
        task = Task(title="Concurrent owner review", assignee="fixture")
        session.add(task)
        session.flush()
        session.add(
            SopRun(id="concurrent-escalation", role="fixture", sop="classify", task_id=task.id)
        )
        task_id = task.id
        session.commit()

    @contextmanager
    def scope():  # type: ignore[no-untyped-def]
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(runner.db, "session_scope", scope)
    monkeypatch.setattr(
        runner,
        "ctx",
        lambda: SimpleNamespace(bus=Bus(schemas), evidence=SimpleNamespace(note=evidence.note)),
    )
    trigger = {"id": str(uuid.uuid4()), "summary": "Request needing review"}
    ready = Barrier(2)

    def repeat():  # type: ignore[no-untyped-def]
        ready.wait(timeout=10)
        inspect.unwrap(runner._raise_escalation)(
            "concurrent-escalation", "fixture", trigger, "A tool signalled owner review."
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: repeat(), range(2)))
        with Session(engine) as session:
            packet = session.scalars(select(Packet).where(Packet.type == "escalation.raised")).one()
            activity = session.scalars(
                select(EvidenceEvent).where(EvidenceEvent.task_id == task_id)
            ).one()
            assert activity.ref == str(packet.id)
            assert session.scalars(
                select(AuditEntry).where(AuditEntry.kind == "sop.escalate_when")
            ).one()
    finally:
        engine.dispose()
