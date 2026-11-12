"""Pause events. The newest row is the current state; rows are never changed."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES
from biz_kernel.db import Base, utcnow, uuid7


class PauseEvent(Base):
    __tablename__ = "control_pauses"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    paused: Mapped[bool] = mapped_column(Boolean)
    by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")


APPEND_ONLY_TABLES.append("control_pauses")
