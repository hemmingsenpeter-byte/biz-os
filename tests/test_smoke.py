import pytest
from biz_kernel.audit.log import record
from biz_kernel.packets.bus import Bus, SchemaError, SchemaRegistry, timeline
from biz_kernel.records.models import Job
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_publish_validates_and_lands_on_timeline(session):
    reg = SchemaRegistry()
    reg.register(
        "thing.done",
        1,
        {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}},
    )
    bus = Bus(reg)
    seen = []
    bus.subscribe("thing.done", "watcher", lambda s, p: seen.append(p.id))
    job = Job(kind="k", stage="new")
    session.add(job)
    session.flush()
    p = bus.publish(session, type="thing.done", payload={"n": 1}, from_part="t", job_id=job.id)
    assert seen == [p.id] and p.to_parts == ["watcher"]
    assert [x.id for x in timeline(session, job.id)] == [p.id]
    with pytest.raises(SchemaError):
        bus.publish(session, type="thing.done", payload={"n": "x"}, from_part="t")


def test_audit_rejects_update(session):
    e = record(session, "me", "test")
    with pytest.raises(DBAPIError):
        session.execute(text("UPDATE audit_log SET actor='x' WHERE id=:i"), {"i": e.id})
        session.flush()
