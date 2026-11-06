"""memory.* tools. The caller's readable scopes come from its role pack, never from the call."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from sqlalchemy.orm import Session

from biz_kernel.memory.embedder import Embedder
from biz_kernel.memory.service import recall
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry

RECALL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "scopes": {"type": "array", "items": {"type": "string"}},
        "k": {"type": "integer", "minimum": 1, "maximum": 10},
    },
}


def register(
    registry: ToolRegistry, role_sources: Mapping[str, list[str]], embedder: Embedder
) -> None:
    def recall_tool(
        session: Session, actor: str, query: str, scopes: list[str] | None = None, k: int = 5
    ) -> list[dict[str, Any]]:
        sources = list(role_sources.get(actor, []))
        wanted = scopes or sources
        hits = recall(session, query, wanted, sources, embedder, k=k)
        out = []
        for h in hits:
            row = asdict(h)
            row["note_id"] = str(h.note_id)
            out.append(row)
        return out

    registry.register(
        Tool(
            "memory.recall",
            "kernel",
            SideEffect.READ,
            RECALL_SCHEMA,
            recall_tool,
            "Recall short cited excerpts from shared memory",
            wants_actor=True,
        )
    )
