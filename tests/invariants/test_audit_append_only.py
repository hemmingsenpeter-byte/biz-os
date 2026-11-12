"""UPDATE or DELETE on append-only tables fails at the database, whatever the caller."""

import pytest
from biz_kernel.audit.log import APPEND_ONLY_TABLES, record
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import_all_models()  # every module appends its tables before we parametrize


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
    if table in ("memory_sources", "memory_writes"):
        from biz_kernel.memory import Cite, HashEmbedder, ingest_source, write_draft

        src = ingest_source(session, "doc", "file://t.md", "Plain fact for the test.")
        if table == "memory_sources":
            return src.id
        write_draft(
            session,
            "t",
            "business",
            "fact",
            "T",
            "Plain fact.",
            [Cite(src.id, "Plain fact", None)],
            HashEmbedder(768),
        )
        return session.execute(text("SELECT id FROM memory_writes LIMIT 1")).scalar()
    if table == "control_pauses":
        from biz_kernel import control
        from biz_kernel.control.models import PauseEvent

        control.pause(session, "tester")
        return session.scalar(select(PauseEvent.id))
    if table == "approval_corrections":
        from biz_kernel.approvals import staged
        from biz_kernel.approvals.models import Correction
        from biz_kernel.policy.types import Action, SideEffect

        action = Action(actor="r", tool="t.send", side_effect=SideEffect.EXTERNAL, inputs={"a": 1})
        op = staged.stage(session, action, "owner")
        staged.edit(session, op.id, {"a": 2}, by="owner", severity="factual")
        return session.scalar(select(Correction.id))
    return session.execute(text(f"SELECT id FROM {table} LIMIT 1")).scalar()


@pytest.mark.parametrize(
    "table", ["audit_log", "packets", "evidence_events", "memory_sources", "memory_writes"]
)
def test_core_tables_are_registered(table):
    assert table in APPEND_ONLY_TABLES


@pytest.mark.parametrize(
    "stmt", ["UPDATE {t} SET id = id WHERE id = :i", "DELETE FROM {t} WHERE id = :i"]
)
@pytest.mark.parametrize("table", sorted(set(APPEND_ONLY_TABLES)))
def test_audit_append_only(session, table, stmt):
    row_id = _row(session, table)
    assert row_id is not None, f"add a row factory for {table} to _row()"
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text(stmt.format(t=table)), {"i": row_id})
