"""Task activity exposes only evidence applicable to the requested task."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from biz_kernel.auth import tokens
from biz_kernel.auth.users import create_user
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.records.models import Job, Task


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
