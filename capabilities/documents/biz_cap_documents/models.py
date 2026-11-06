"""Documents tables, prefixed cap_documents_. The versioned file record is the kernel Document."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Render(Base):
    """How a Document came to be: the content hash of the rendered source, for idempotency."""

    __tablename__ = "cap_documents_renders"
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    template: Mapped[str | None] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(10))  # pdf | html | file
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Signature(Base):
    __tablename__ = "cap_documents_signatures"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    system: Mapped[str] = mapped_column(String(40))
    request_id: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(
        String(12), default="requested"
    )  # requested|signed|declined
    signers: Mapped[list[Any]] = mapped_column(JSONB)
    signed_by: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    signed_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]
