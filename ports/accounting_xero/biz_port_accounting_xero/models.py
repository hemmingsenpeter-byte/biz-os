"""Durable post ledger for this port: a draft invoice or bill is created once per key."""

from __future__ import annotations

from datetime import datetime

from biz_kernel.db import Base, utcnow
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class XeroPost(Base):
    __tablename__ = "port_xero_posts"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # sending | sent
    provider_id: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(default=utcnow)
