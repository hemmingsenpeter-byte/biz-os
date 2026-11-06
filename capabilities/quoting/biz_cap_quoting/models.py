"""Quoting tables, prefixed cap_quoting_. Versioned rows are superseded, never overwritten."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

MONEY = Numeric(16, 2)
UNIT = Numeric(16, 4)


class Brief(Base):
    __tablename__ = "cap_quoting_briefs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    summary: Mapped[str] = mapped_column(Text)
    requirements: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    constraints: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    budget_hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Survey(Base):
    __tablename__ = "cap_quoting_surveys"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    checklist_answers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    measurements: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Scope(Base):
    __tablename__ = "cap_quoting_scopes"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_quoting_scope_version"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(Integer)
    areas: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    exclusions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    assumptions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(12), default="current")  # current | superseded
    superseded_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PriceItem(Base):
    __tablename__ = "cap_quoting_price_items"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    description: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(30))
    unit_cost: Mapped[Decimal] = mapped_column(UNIT)
    unit_price: Mapped[Decimal] = mapped_column(UNIT)
    source: Mapped[str] = mapped_column(String(200), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class PriceHistory(Base):
    """Superseded values of a price item: one row each time an import changed it."""

    __tablename__ = "cap_quoting_price_history"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(80), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(30))
    unit_cost: Mapped[Decimal] = mapped_column(UNIT)
    unit_price: Mapped[Decimal] = mapped_column(UNIT)
    source: Mapped[str] = mapped_column(String(200), default="")
    valid_from: Mapped[datetime]
    superseded_at: Mapped[datetime] = mapped_column(default=utcnow)


class Estimate(Base):
    __tablename__ = "cap_quoting_estimates"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_quoting_estimate_version"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cap_quoting_scopes.id"))
    version: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    contingency_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal(0))
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal(0))
    total_cost: Mapped[Decimal] = mapped_column(MONEY)
    total_price: Mapped[Decimal] = mapped_column(MONEY)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    margin_ok: Mapped[bool] = mapped_column(Boolean)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(12), default="current")
    superseded_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EstimateLine(Base):
    __tablename__ = "cap_quoting_estimate_lines"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    estimate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cap_quoting_estimates.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(UNIT)
    unit: Mapped[str] = mapped_column(String(30))
    unit_cost: Mapped[Decimal] = mapped_column(UNIT)
    unit_price: Mapped[Decimal] = mapped_column(UNIT)
    line_cost: Mapped[Decimal] = mapped_column(MONEY)
    line_price: Mapped[Decimal] = mapped_column(MONEY)
    unpriced: Mapped[bool] = mapped_column(Boolean, default=False)
    # Hook for estimate-vs-actual learning: where the cost came from at estimate time.
    cost_basis: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Proposal(Base):
    __tablename__ = "cap_quoting_proposals"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_quoting_proposal_version"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    estimate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cap_quoting_estimates.id"))
    version: Mapped[int] = mapped_column(Integer)
    valid_until: Mapped[datetime]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="current")
    superseded_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Variation(Base):
    __tablename__ = "cap_quoting_variations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3))
    lines: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal(0))
    total_cost: Mapped[Decimal] = mapped_column(MONEY)
    total_price: Mapped[Decimal] = mapped_column(MONEY)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    margin_ok: Mapped[bool] = mapped_column(Boolean)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
