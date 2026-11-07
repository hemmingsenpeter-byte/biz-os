"""Invoicing tables, prefixed cap_invoicing_. A posted invoice is never edited."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

MONEY = Numeric(16, 2)
PCT = Numeric(7, 4)


class Stage(Base):
    """One payment stage of a job: a share of the job value, or a fixed amount."""

    __tablename__ = "cap_invoicing_stages"
    __table_args__ = (UniqueConstraint("job_id", "name", name="uq_invoicing_stage_name"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)
    pct: Mapped[Decimal | None] = mapped_column(PCT)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Invoice(Base):
    __tablename__ = "cap_invoicing_invoices"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    party_id: Mapped[uuid.UUID]
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)  # one per milestone
    stage: Mapped[str | None] = mapped_column(String(120))
    reference: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    lines: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    total: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft | posted | paid
    paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    system: Mapped[str | None] = mapped_column(String(40))
    external_id: Mapped[str | None] = mapped_column(String(120))
    due: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    posted_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)


class ReceivedPayment(Base):
    """A payment seen in the accounting system, recorded once by its external id."""

    __tablename__ = "cap_invoicing_payments"
    __table_args__ = (UniqueConstraint("system", "external_id", name="uq_invoicing_payment_ext"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    system: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(120))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    paid_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
