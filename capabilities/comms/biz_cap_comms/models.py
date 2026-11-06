"""Comms tables, prefixed cap_comms_."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Outbox(Base):
    __tablename__ = "cap_comms_outbox"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    channel: Mapped[str] = mapped_column(String(10))  # mail | sms
    to: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # draft | queued | sending | sent | uncertain | failed | blocked
    status: Mapped[str] = mapped_column(String(12), default="draft")
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    party_id: Mapped[uuid.UUID | None]
    expected_to: Mapped[str | None] = mapped_column(String(320))
    gate_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None]


class OptOut(Base):
    """Permanent suppression. Never deleted by this capability."""

    __tablename__ = "cap_comms_optouts"
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    party_id: Mapped[uuid.UUID | None]
    source: Mapped[str] = mapped_column(String(40), default="")
    at: Mapped[datetime] = mapped_column(default=utcnow)
