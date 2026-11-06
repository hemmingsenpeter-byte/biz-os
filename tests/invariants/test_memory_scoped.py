"""A role can't recall notes outside its declared sources, whatever scopes it asks for."""

from biz_kernel.memory import Cite, HashEmbedder, curate, ingest_source, recall, write_draft

E = HashEmbedder()


def test_memory_scoped(session):
    src = ingest_source(session, "document", "d", "Secret margin is large.")
    for scope in ("business", "job:1", "role:owner"):
        n = write_draft(
            session,
            "t",
            scope,
            "fact",
            f"margin {scope}",
            "margin secret",
            [Cite(src.id, "margin is large")],
            E,
        )
        curate(session, n.id, "owner")
    res = recall(
        session, "margin", ["business", "job:1", "role:owner", "*"], ["business", "job:*"], E
    )
    assert {r.title for r in res} == {"margin business", "margin job:1"}
    assert recall(session, "margin", ["role:owner"], ["business"], E) == []
    assert recall(session, "margin", ["business"], [], E) == []
