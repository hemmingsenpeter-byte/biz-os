"""Task activity exposes only evidence applicable to the requested task."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from biz_kernel.audit.log import record
from biz_kernel.auth import tokens
from biz_kernel.auth.users import create_user
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Job, Task
from biz_kernel.tools.shadow import SHADOW_EVIDENCE


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
        "subject": None,
        "reason": None,
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


@pytest.mark.parametrize("canonical_ref", [False, True], ids=["uuid", "uppercase-uuid"])
def test_linked_escalation_exposes_only_its_subject_and_reason(
    client, session, canonical_ref: bool
) -> None:
    user = create_user(session, "owner@example.invalid", "owner", "Owner")
    job = Job(kind="work", stage="open")
    session.add(job)
    session.flush()
    task = Task(title="Needs the owner", status="open", job_id=job.id)
    session.add(task)
    escalation = Packet(
        type="escalation.raised",
        schema_version=1,
        from_part="fixture",
        job_id=job.id,
        payload={
            "subject": "Review uncertain classification",
            "reason": "Confidence is below the required threshold.",
            "raised_by": "fixture",
            "private_extra": "Never expose the full packet payload.",
        },
    )
    session.add(escalation)
    session.flush()
    reference = str(escalation.id).upper() if canonical_ref else str(escalation.id)
    session.add(
        EvidenceEvent(
            kind="packet:escalation.raised", ref=reference, task_id=task.id, job_id=job.id
        )
    )
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    response = client.get(f"/v1/tasks/{task.id}/activity")
    assert response.status_code == 200, response.text
    (event,) = response.json()
    assert event["subject"] == escalation.payload["subject"]
    assert event["reason"] == escalation.payload["reason"]
    assert event["shadow"] is None
    assert "Never expose" not in response.text


@pytest.mark.parametrize(
    "mismatch", ["other-job", "other-packet-type", "other-evidence-kind", "invalid-ref", "missing"]
)
def test_escalation_detail_requires_matching_scope_type_and_reference(
    client, session, mismatch: str
) -> None:
    user = create_user(session, "reader@example.invalid", "staff", "Reader")
    job, other_job = Job(kind="work", stage="open"), Job(kind="work", stage="open")
    session.add_all([job, other_job])
    session.flush()
    task = Task(title="Own task", status="open", job_id=job.id)
    session.add(task)
    packet = Packet(
        type="message.sent" if mismatch == "other-packet-type" else "escalation.raised",
        schema_version=1,
        from_part="fixture",
        job_id=other_job.id if mismatch == "other-job" else job.id,
        payload={"subject": "Private unrelated subject", "reason": "Private unrelated reason"},
    )
    session.add(packet)
    session.flush()
    reference = (
        "invalid-reference"
        if mismatch == "invalid-ref"
        else str(uuid.uuid4())
        if mismatch == "missing"
        else str(packet.id)
    )
    session.add(
        EvidenceEvent(
            kind="other-event" if mismatch == "other-evidence-kind" else "packet:escalation.raised",
            ref=reference,
            task_id=task.id,
            job_id=job.id,
        )
    )
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    response = client.get(f"/v1/tasks/{task.id}/activity")
    assert response.status_code == 200, response.text
    (event,) = response.json()
    assert event["subject"] is None and event["reason"] is None
    assert "Private unrelated" not in response.text


def test_shadow_activity_is_a_selective_view_of_its_matched_audit_record(client, session) -> None:
    user = create_user(session, "reader@example.invalid", "staff", "Reader")
    task, sibling = Task(title="Own task", status="open"), Task(title="Sibling", status="open")
    session.add_all([task, sibling])
    session.flush()
    suppression = record(
        session,
        "fixture",
        SHADOW_EVIDENCE,
        "comms.send",
        effects=["external"],
        policy={"decision": "ask", "reason": "Owner approval is required."},
        pre_approved=False,
        staged_op_id="staged-reference",
        recipients=["private-recipient@example.invalid"],
        references={"message_id": "private-message-reference"},
        body="Private message body.",
    )
    unrelated = record(
        session,
        "fixture",
        SHADOW_EVIDENCE,
        "private.unrelated_tool",
        effects=["money"],
        policy={"decision": "deny", "reason": "Private unrelated decision."},
    )
    session.add_all(
        [
            EvidenceEvent(kind=SHADOW_EVIDENCE, ref=str(suppression.id).upper(), task_id=task.id),
            EvidenceEvent(kind=SHADOW_EVIDENCE, ref=str(unrelated.id), task_id=sibling.id),
        ]
    )
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    response = client.get(f"/v1/tasks/{task.id}/activity")
    assert response.status_code == 200, response.text
    (event,) = response.json()
    assert event["shadow"] == {
        "tool": "comms.send",
        "effects": ["external"],
        "policy": {"decision": "ask", "reason": "Owner approval is required."},
        "pre_approved": False,
        "staged_op_id": "staged-reference",
    }
    assert event["subject"] is None and event["reason"] is None
    for private in (
        "private-recipient",
        "private-message-reference",
        "Private message body",
        "private.unrelated_tool",
        "Private unrelated decision",
    ):
        assert private not in response.text


@pytest.mark.parametrize("mismatch", ["audit-kind", "evidence-kind", "invalid-ref", "missing"])
def test_shadow_detail_needs_matching_audit_and_evidence_kinds(
    client, session, mismatch: str
) -> None:
    user = create_user(session, "reader@example.invalid", "staff", "Reader")
    task = Task(title="Own task", status="open")
    session.add(task)
    entry = record(
        session,
        "fixture",
        "unrelated.audit" if mismatch == "audit-kind" else SHADOW_EVIDENCE,
        "private.tool",
        policy={"decision": "deny", "reason": "Do not expose unmatched audit detail."},
    )
    reference = (
        "invalid-reference"
        if mismatch == "invalid-ref"
        else str(uuid.uuid4())
        if mismatch == "missing"
        else str(entry.id)
    )
    session.flush()
    session.add(
        EvidenceEvent(
            kind="other-event" if mismatch == "evidence-kind" else SHADOW_EVIDENCE,
            ref=reference,
            task_id=task.id,
        )
    )
    session.flush()
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    response = client.get(f"/v1/tasks/{task.id}/activity")
    assert response.status_code == 200, response.text
    assert response.json()[0]["shadow"] is None
    assert "private.tool" not in response.text
    assert "Do not expose" not in response.text
