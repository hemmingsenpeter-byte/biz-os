"""Durable send ledger table for this port."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from biz_kernel.db import Base, utcnow
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class TwilioSend(Base):
    __tablename__ = "port_twilio_sends"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # sending | sent | failed | uncertain
    provider_id: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(default=utcnow)
