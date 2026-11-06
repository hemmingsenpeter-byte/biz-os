"""Capability tables are prefixed cap_<name>_."""

from __future__ import annotations

import uuid
from datetime import datetime

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column


class DemoRequest(Base):
    __tablename__ = "cap_demo_desk_requests"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    sender: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class DemoDraft(Base):
    __tablename__ = "cap_demo_desk_drafts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    request_id: Mapped[uuid.UUID]
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
