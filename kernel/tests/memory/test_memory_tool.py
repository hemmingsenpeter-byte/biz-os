import jsonschema
import pytest
from biz_kernel.memory import Cite, HashEmbedder, curate, ingest_source, write_draft
from biz_kernel.memory.tools import register
from biz_kernel.policy.types import Allow
from biz_kernel.tools.registry import ToolRegistry


class AllowAll:
    def check(self, session, action):
        return Allow()


def test_recall_tool_scopes_come_from_role_pack(session):
    emb = HashEmbedder(768)
    reg = ToolRegistry()
    register(reg, {"clerk": ["business"], "other": ["role:other"]}, emb)
    src = ingest_source(session, "doc", "file://hours.md", "We open at nine on weekdays.")
    for scope in ("business", "role:secret"):
        note = write_draft(
            session,
            "seed",
            scope,
            "fact",
            f"Hours {scope}",
            "We open at nine on weekdays.",
            [Cite(src.id, "We open at nine", "line 1")],
            emb,
        )
        curate(session, note.id, "owner")

    hits = reg.call(
        session,
        AllowAll(),
        actor="clerk",
        name="memory.recall",
        args={"query": "when do we open", "scopes": ["business", "role:secret"]},
    ).output
    assert hits and all(h["title"] == "Hours business" for h in hits)
    assert all(h["quote"] and h["source_uri"] for h in hits)
    assert (
        reg.call(
            session, AllowAll(), actor="other", name="memory.recall", args={"query": "open"}
        ).output
        == []
    )


def test_caller_cannot_claim_an_actor(session):
    reg = ToolRegistry()
    register(reg, {"clerk": ["business"]}, HashEmbedder(768))
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        reg.call(
            session,
            AllowAll(),
            actor="clerk",
            name="memory.recall",
            args={"query": "x", "actor": "owner"},
        )
