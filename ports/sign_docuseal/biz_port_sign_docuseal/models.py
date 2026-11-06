"""Durable request ledger table for this port."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from biz_kernel.db import Base, utcnow
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class DocuSealRequest(Base):
    __tablename__ = "port_docuseal_requests"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # sending | sent
    provider_id: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(default=utcnow)
