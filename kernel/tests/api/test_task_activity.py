"""Task activity exposes only evidence applicable to the requested task."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from biz_kernel.audit.log import record
from biz_kernel.auth import tokens
from biz_kernel.auth.users import create_user
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.policy.types import SideEffect
from biz_kernel.records.models import Job, Task
from biz_kernel.tools.shadow import SHADOW_EVIDENCE, record_suppression


def test_activity_requires_sign_in_even_when_task_is_missing(client) -> None:
    assert client.get(f"/v1/tasks/{uuid.uuid4()}/activity").status_code == 401


def test_activity_empty_and_missing_task(client, session) -> None:
    user = create_user(session, "reader@example.invalid", "staff", "Reader")
    task = Task(title="Empty", status="open")
    session.add(task)
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    assert client.get(f"/v1/tasks/{task.id}/activity").json() == []
    assert client.get(f"/v1/tasks/{uuid.uuid4()}/activity").status_code == 404


def test_activity_scopes_and_orders_completion_evidence(client, session) -> None:
    user = create_user(session, "owner@example.invalid", "owner", "Owner")
    job, other_job = Job(kind="work", stage="open"), Job(kind="work", stage="open")
    session.add_all([job, other_job])
    session.flush()
    task = Task(title="Review", job_id=job.id, status="open")
    sibling = Task(title="Other task", job_id=job.id, status="open")
    session.add_all([task, sibling])
    session.flush()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    task_event = EvidenceEvent(
        kind="approval.granted", ref="approved-op", task_id=task.id, job_id=job.id, at=now
    )
    job_event = EvidenceEvent(
        kind="packet:message.sent", ref="packet-id", job_id=job.id, at=now + timedelta(seconds=1)
    )
    session.add_all(
        [
            job_event,
            task_event,
            EvidenceEvent(kind="sibling", task_id=sibling.id, job_id=job.id, at=now),
            EvidenceEvent(kind="unrelated-job", job_id=other_job.id, at=now),
            EvidenceEvent(kind="unassigned", at=now),
        ]
    )
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    response = client.get(f"/v1/tasks/{task.id}/activity")
    assert response.status_code == 200
    activity = response.json()
    assert [event["id"] for event in activity] == [str(task_event.id), str(job_event.id)]
    assert activity[0] == {
        "id": str(task_event.id),
        "kind": "approval.granted",
        "ref": "approved-op",
        "task_id": str(task.id),
        "job_id": str(job.id),
        "at": "2030-01-01T00:00:00Z",
        "shadow": None,
    }
    assert activity[1]["task_id"] is None


def test_task_without_job_does_not_inherit_unassigned_evidence(client, session) -> None:
    user = create_user(session, "reader@example.invalid", "staff", "Reader")
    task = Task(title="No job", status="open")
    session.add(task)
    session.flush()
    own = EvidenceEvent(kind="own", task_id=task.id)
    session.add_all([own, EvidenceEvent(kind="unassigned")])
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    activity = client.get(f"/v1/tasks/{task.id}/activity").json()
    assert [event["id"] for event in activity] == [str(own.id)]


def test_shadow_evidence_carries_suppressed_tool_and_would_be_verdict(client, session) -> None:
    user = create_user(session, "owner@example.invalid", "owner", "Owner")
    task = Task(title="Shadowed", status="open")
    session.add(task)
    session.flush()
    suppression = record_suppression(
        session,
        "some_role",
        "comms.send",
        {SideEffect.EXTERNAL},
        {"to": "a@example.invalid", "body": "secret body <b>x</b>"},
        decision_type="offer_sending",
        policy={"decision": "ask", "reason": "<script>first send</script>"},
        pre_approved=True,
        staged_op_id="op-1",
    )
    unevaluated = record_suppression(session, "some_role", "pay.bill", {SideEffect.MONEY}, {})
    not_a_suppression = record(session, "some_role", "tool_call", "comms.send")
    events = [
        EvidenceEvent(kind=SHADOW_EVIDENCE, ref=str(suppression.audit_id), task_id=task.id),
        EvidenceEvent(kind=SHADOW_EVIDENCE, ref=str(unevaluated.audit_id), task_id=task.id),
        EvidenceEvent(kind=SHADOW_EVIDENCE, ref=str(not_a_suppression.id), task_id=task.id),
        EvidenceEvent(kind=SHADOW_EVIDENCE, ref="not-a-uuid", task_id=task.id),
        EvidenceEvent(kind=SHADOW_EVIDENCE, task_id=task.id),
        EvidenceEvent(kind="approval.granted", ref=str(suppression.audit_id), task_id=task.id),
    ]
    now = datetime(2030, 1, 1, tzinfo=UTC)
    for offset, event in enumerate(events):
        event.at = now + timedelta(seconds=offset)
    session.add_all(events)
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    activity = client.get(f"/v1/tasks/{task.id}/activity").json()
    assert [event["id"] for event in activity] == [str(event.id) for event in events]
    assert activity[0]["shadow"] == {
        "tool": "comms.send",
        "effects": ["external"],
        "policy": {"decision": "ask", "reason": "<script>first send</script>"},
        "pre_approved": True,
        "staged_op_id": "op-1",
    }
    assert "secret body" not in str(activity)
    assert activity[1]["shadow"]["tool"] == "pay.bill"
    assert activity[1]["shadow"]["policy"] is None
    assert activity[1]["shadow"]["pre_approved"] is False
    assert [event["shadow"] for event in activity[2:]] == [None] * 4
