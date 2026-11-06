"""Memory tables: sources (immutable), notes, cites, links and the write ledger."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES
from biz_kernel.db import Base, utcnow, uuid7

EMBED_DIM = int(os.environ.get("BIZ_EMBED_DIM", "768"))

# Sources are never edited; the write ledger is never rewritten.
for _t in ("memory_sources", "memory_writes"):
    if _t not in APPEND_ONLY_TABLES:
        APPEND_ONLY_TABLES.append(_t)


class MemorySource(Base):
    __tablename__ = "memory_sources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(40))  # email | document | voice_note | sop | ...
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(default=utcnow)


class MemoryNote(Base):
    __tablename__ = "memory_notes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    scope: Mapped[str] = mapped_column(String(200))  # business | job:<id> | party:<id> | role:<n>
    type: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft|curated|retired
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || coalesce(body, ''))",
            persisted=True,
        ),
    )
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        Index("ix_memory_notes_search", "search", postgresql_using="gin"),
        Index(
            "ix_memory_notes_scope", "scope", "status", postgresql_ops={"scope": "text_pattern_ops"}
        ),
    )


class MemoryCite(Base):
    __tablename__ = "memory_cites"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_notes.id"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_sources.id"))
    quote: Mapped[str] = mapped_column(Text)
    locator: Mapped[str | None] = mapped_column(Text)


class MemoryLink(Base):
    __tablename__ = "memory_links"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    from_note: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_notes.id"), index=True)
    to_note: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_notes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))


class MemoryWrite(Base):
    __tablename__ = "memory_writes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_notes.id"), index=True)
    op: Mapped[str] = mapped_column(String(20))  # draft | update | retire | curate | undo
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(120))
    at: Mapped[datetime] = mapped_column(default=utcnow)
