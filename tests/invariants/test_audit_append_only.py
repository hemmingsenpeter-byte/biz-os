"""UPDATE or DELETE on append-only tables fails at the database, whatever the caller."""

import pytest
from biz_kernel.audit.log import APPEND_ONLY_TABLES, record
from biz_kernel.packets.bus import Packet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def _row(session, table: str):
    if table == "audit_log":
        return record(session, "tester", "test").id
    if table == "packets":
        p = Packet(type="x", schema_version=1, from_part="t", payload={})
        session.add(p)
        session.flush()
        return p.id
    if table == "evidence_events":
        from biz_kernel.evidence.service import note

        return note(session, "test.evidence").id
    return session.execute(text(f"SELECT id FROM {table} LIMIT 1")).scalar()


@pytest.mark.parametrize("table", ["audit_log", "packets"])
def test_core_tables_are_registered(table):
    assert table in APPEND_ONLY_TABLES


@pytest.mark.parametrize(
    "stmt", ["UPDATE {t} SET id = id WHERE id = :i", "DELETE FROM {t} WHERE id = :i"]
)
@pytest.mark.parametrize("table", sorted(set(APPEND_ONLY_TABLES)))
def test_audit_append_only(session, table, stmt):
    row_id = _row(session, table)
    if row_id is None:
        pytest.skip(f"no row to mutate in {table}")
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text(stmt.format(t=table)), {"i": row_id})
