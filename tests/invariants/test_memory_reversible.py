"""Every memory write can be undone; sources are never modified."""

import pytest
from biz_kernel.memory import (
    Cite,
    HashEmbedder,
    ingest_source,
    retire_note,
    undo,
    update_note,
    write_draft,
)
from biz_kernel.memory.models import MemoryWrite
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

E = HashEmbedder()


def test_memory_reversible(session):
    src = ingest_source(session, "document", "d", "Quote lives here.")
    n = write_draft(session, "t", "business", "fact", "T0", "B0", [Cite(src.id, "lives here")], E)
    update_note(session, n.id, "t", E, title="T1", body="B1")
    retire_note(session, n.id, "t")
    writes = session.scalars(
        select(MemoryWrite).where(MemoryWrite.note_id == n.id).order_by(MemoryWrite.id.desc())
    ).all()
    for w in writes:  # undo newest first
        undo(session, w.id, "t")
    assert n.status == "retired"  # creation undone
    assert (n.title, n.body) == ("T0", "B0")


def test_sources_never_modified(session):
    src = ingest_source(session, "document", "d", "Fixed text.")
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(
            text("UPDATE memory_sources SET content = 'x' WHERE id = :i"), {"i": src.id}
        )
