import pytest
from biz_kernel.evidence import note
from sqlalchemy.exc import DBAPIError


def test_evidence_is_append_only(session):
    ev = note(session, "approval.granted", "x")
    with pytest.raises(DBAPIError):
        session.execute(__import__("sqlalchemy").text("DELETE FROM evidence_events"))
    assert ev.id
