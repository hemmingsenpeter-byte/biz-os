import pytest
from biz_kernel.memory import (
    Cite,
    HashEmbedder,
    curate,
    ingest_source,
    load_profile,
    recall,
    retire_note,
    undo,
    update_note,
    write_draft,
)
from biz_kernel.memory.models import MemoryNote, MemoryWrite
from biz_kernel.memory.service import EXCERPT_MAX, MemoryRuleError
from sqlalchemy import select

E = HashEmbedder()
TEXT = "The boiler needs servicing every year. Filters are replaced in spring."


def mk(
    session, scope="business", title="Boiler", body="Boiler servicing happens yearly.", cur=True
):
    src = ingest_source(session, "document", "doc:1", TEXT)
    n = write_draft(
        session, "t", scope, "fact", title, body, [Cite(src.id, "servicing every year", "p1")], E
    )
    if cur:
        curate(session, n.id, "owner")
    return n


def test_dedupe(session):
    a = ingest_source(session, "document", "u1", "same")
    b = ingest_source(session, "email", "u2", b"same")
    assert a.id == b.id


def test_bad_quote_refused(session):
    src = ingest_source(session, "document", "u", TEXT)
    with pytest.raises(MemoryRuleError):
        write_draft(session, "t", "business", "fact", "x", "y", [Cite(src.id, "not there")], E)


def test_undo_of_undo_and_retire(session):
    n = mk(session)
    update_note(session, n.id, "t", E, body="Changed body.")
    assert n.status == "draft"
    last = session.scalars(
        select(MemoryWrite).where(MemoryWrite.note_id == n.id).order_by(MemoryWrite.id.desc())
    ).first()
    undo(session, last.id, "t")
    assert n.body == "Boiler servicing happens yearly." and n.status == "curated"
    last = session.scalars(
        select(MemoryWrite).where(MemoryWrite.note_id == n.id).order_by(MemoryWrite.id.desc())
    ).first()
    assert last.op == "undo"
    undo(session, last.id, "t")  # undo the undo
    assert n.body == "Changed body."
    retire_note(session, n.id, "t")
    assert n.status == "retired"
    assert recall(session, "boiler", ["business"], ["business"], E) == []


def test_undo_creation_retires(session):
    n = mk(session, cur=False)
    w = session.scalars(select(MemoryWrite).where(MemoryWrite.note_id == n.id)).first()
    undo(session, w.id, "t")
    assert n.status == "retired"


def test_drafts_hidden_by_default(session):
    mk(session, cur=False)
    assert recall(session, "boiler", ["business"], ["business"], E) == []
    assert len(recall(session, "boiler", ["business"], ["business"], E, include_drafts=True)) == 1


def test_ranking_and_excerpt_cap(session):
    src = ingest_source(session, "document", "d", TEXT)
    long_body = "Pipes freeze in winter. " + "Filler sentence about nothing. " * 40
    for title, body in [
        ("Boilers", "Boiler servicing yearly."),
        ("Pipes", long_body),
        ("Vans", "Vans need tyres."),
    ]:
        n = write_draft(
            session, "t", "business", "fact", title, body, [Cite(src.id, "Filters are replaced")], E
        )
        curate(session, n.id, "owner")
    res = recall(session, "boiler servicing", ["business"], ["business"], E)
    assert res[0].title == "Boilers"
    res = recall(session, "pipes freeze", ["business"], ["business"], E)
    assert res[0].title == "Pipes" and len(res[0].excerpt) <= EXCERPT_MAX


def test_wildcard_scopes(session):
    mk(session, scope="job:7")
    assert recall(session, "boiler", ["job:*"], ["job:*"], E)
    assert recall(session, "boiler", ["job:7"], ["job:*", "business"], E)
    assert not recall(session, "boiler", ["job:*"], ["job:8"], E)


def test_load_profile_demo_and_idempotent(session):
    notes = load_profile(session, "apps/demo/memory", E)
    assert len(notes) == 5 and all(n.status == "draft" for n in notes)
    assert load_profile(session, "apps/demo/memory", E) == []
    assert session.scalar(select(MemoryNote).limit(1)) is not None


def test_load_profile_hive_placeholders(session):
    assert load_profile(session, "apps/hive/memory", E)
