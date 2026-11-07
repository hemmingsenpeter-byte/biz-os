"""Money spend ledger: every money tool call that ran writes one row (daily limits read it)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, uuid7


class MoneySpend(Base):
    __tablename__ = "money_spend"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    actor: Mapped[str] = mapped_column(String(120))
    tool: Mapped[str] = mapped_column(String(160))
    decision_type: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    at: Mapped[datetime]

    __table_args__ = (Index("ix_money_spend_at", "at"),)
