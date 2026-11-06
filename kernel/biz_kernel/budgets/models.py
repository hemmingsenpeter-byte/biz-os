"""Cost events: every paid model or tool call writes one."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, uuid7


class CostEvent(Base):
    __tablename__ = "cost_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    role: Mapped[str] = mapped_column(String(120))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    kind: Mapped[str] = mapped_column(String(10))  # model | tool
    ref: Mapped[str | None] = mapped_column(String(240))
    at: Mapped[datetime]

    __table_args__ = (Index("ix_cost_events_role_at", "role", "at"),)
