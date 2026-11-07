"""Payables tables, prefixed cap_payables_. Bank details live only in verified rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

MONEY = Numeric(16, 2)


class PurchaseOrder(Base):
    """A copy of an issued PO (from po.issued), kept for matching bills."""

    __tablename__ = "cap_payables_pos"
    __table_args__ = (UniqueConstraint("supplier_party_id", "number", name="uq_payables_po_num"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # the po_id from po.issued
    supplier_party_id: Mapped[uuid.UUID] = mapped_column(index=True)
    number: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[uuid.UUID | None]
    total: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Bill(Base):
    __tablename__ = "cap_payables_bills"
    __table_args__ = (UniqueConstraint("supplier_party_id", "number", name="uq_payables_bill_num"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    supplier_party_id: Mapped[uuid.UUID] = mapped_column(index=True)
    number: Mapped[str] = mapped_column(String(80))  # the supplier's invoice number
    po_number: Mapped[str | None] = mapped_column(String(80))
    po_id: Mapped[uuid.UUID | None]
    lines: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    total: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    due: Mapped[datetime | None]
    # received | matched | mismatch | posted | in_run
    status: Mapped[str] = mapped_column(String(12), default="received")
    problems: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    system: Mapped[str | None] = mapped_column(String(40))
    external_id: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BankAccount(Base):
    """A supplier's verified bank account. Written only by a confirmed bank change."""

    __tablename__ = "cap_payables_bank_accounts"
    supplier_party_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(64))
    account_name: Mapped[str] = mapped_column(String(200))
    change_id: Mapped[uuid.UUID]
    verified_at: Mapped[datetime]


class BankChange(Base):
    """A requested bank-detail change. It applies only after a verified call-back."""

    __tablename__ = "cap_payables_bank_changes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    supplier_party_id: Mapped[uuid.UUID] = mapped_column(index=True)
    account: Mapped[str] = mapped_column(String(64))
    account_name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(200))  # how the request arrived
    callback_phone: Mapped[str | None] = mapped_column(String(40))  # from our records only
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | applied
    verified_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    applied_at: Mapped[datetime | None]
