"""A task can't be marked done without its declared evidence in the activity log."""

import pytest
from biz_kernel.evidence import EvidenceMissing, complete_task, note
from biz_kernel.records.models import Job, Task

RULES = {"send_quote": ["document.rendered", "approval.granted"], "generic": []}


def test_evidence_bound(session):
    job = Job(kind="x", stage="open")
    session.add(job)
    session.flush()
    task = Task(title="t", kind="send_quote", job_id=job.id)
    session.add(task)
    session.flush()
    with pytest.raises(EvidenceMissing) as e:
        complete_task(session, task.id, RULES)
    assert e.value.missing == ["document.rendered", "approval.granted"]
    note(session, "document.rendered", "doc1", task_id=task.id)
    with pytest.raises(EvidenceMissing) as e:
        complete_task(session, task.id, RULES)
    assert e.value.missing == ["approval.granted"]
    assert task.status == "open"
    note(session, "approval.granted", "ap1", job_id=job.id)  # job-level evidence counts
    complete_task(session, task.id, RULES)
    assert task.status == "done"


def test_no_bypass_and_other_job_evidence_ignored(session):
    j1, j2 = Job(kind="x", stage="o"), Job(kind="x", stage="o")
    session.add_all([j1, j2])
    session.flush()
    task = Task(title="t", kind="generic", job_id=j1.id)
    other = Task(title="u", kind="send_quote", job_id=j1.id)
    session.add_all([task, other])
    session.flush()
    task.status = "done"  # direct write is refused
    with pytest.raises(EvidenceMissing):
        session.flush()
    session.rollback() if False else task.__dict__.update(status="open")
    session.expire(task)
    note(session, "document.rendered", None, job_id=j2.id)
    note(session, "approval.granted", None, job_id=j2.id)
    with pytest.raises(EvidenceMissing):
        complete_task(session, other.id, RULES)
    with pytest.raises(EvidenceMissing):  # undeclared kind fails closed
        complete_task(session, task.id, {})


def test_bulk_update_cannot_bypass_evidence(session):
    """Review finding 8: the database itself refuses done-without-complete_task."""
    import pytest
    from biz_kernel.evidence.service import note
    from biz_kernel.records.models import Task
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    task = Task(title="t", kind="k")
    session.add(task)
    session.flush()
    note(session, "anything", "ref", task_id=task.id)
    with pytest.raises(DBAPIError, match="complete_task"), session.begin_nested():
        session.execute(update(Task).where(Task.id == task.id).values(status="done"))
