"""Booking tables, prefixed cap_booking_."""

from __future__ import annotations

import uuid
from datetime import datetime

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column


class Meeting(Base):
    __tablename__ = "cap_booking_meetings"
    __table_args__ = (
        Index(
            "uq_cap_booking_active_start",
            "starts_at",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    party_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    external_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
