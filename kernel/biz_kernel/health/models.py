"""Schedules and alerts: every recurring job reports its last run; silence is a failure."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, uuid7


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    every_seconds: Mapped[int]
    registered_at: Mapped[datetime]
    last_run_at: Mapped[datetime | None]
    last_ok: Mapped[bool | None]


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(60))
    subject: Mapped[str] = mapped_column(String(240))
    message: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime]
    acknowledged: Mapped[bool] = mapped_column(default=False)
