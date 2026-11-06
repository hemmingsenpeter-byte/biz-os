"""Every recalled excerpt carries a source and quote; a note without a citation can't be curated."""

import pytest
from biz_kernel.memory import Cite, HashEmbedder, curate, ingest_source, recall, write_draft
from biz_kernel.memory.models import MemoryCite, MemoryNote
from biz_kernel.memory.service import MemoryRuleError

E = HashEmbedder()


def test_memory_cited(session):
    src = ingest_source(session, "document", "doc:a", "Tap washers last about five years.")
    n = write_draft(
        session,
        "t",
        "business",
        "fact",
        "Washers",
        "Washers last five years.",
        [Cite(src.id, "last about five years", "l1")],
        E,
    )
    curate(session, n.id, "owner")
    res = recall(session, "washers", ["business"], ["business"], E)
    assert res
    for r in res:
        assert r.source_uri and r.quote and r.quote in src.content


def test_uncited_note_cannot_be_curated(session):
    n = MemoryNote(scope="business", type="fact", title="t", body="b", created_by="x")
    session.add(n)
    session.flush()
    with pytest.raises(MemoryRuleError):
        curate(session, n.id, "owner")
    with pytest.raises(MemoryRuleError):
        write_draft(session, "t", "business", "fact", "t", "b", [], E)
    assert session.query(MemoryCite).filter_by(note_id=n.id).count() == 0
