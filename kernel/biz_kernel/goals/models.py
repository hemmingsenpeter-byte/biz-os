"""Goal tree and readings."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, uuid7


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("goals.id"))
    owner_role: Mapped[str | None] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(120), unique=True)
    target: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    period: Mapped[str] = mapped_column(String(20), default="month")
    unit: Mapped[str] = mapped_column(String(40), default="")
    direction: Mapped[str] = mapped_column(String(10), default="higher")  # higher | lower


class GoalReading(Base):
    __tablename__ = "goal_readings"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id"))
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    at: Mapped[datetime]
    source: Mapped[str | None] = mapped_column(String(160))
