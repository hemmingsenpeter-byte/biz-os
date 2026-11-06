"""Intake tables, prefixed cap_intake_. Bodies are untrusted text, stored only as data."""

from __future__ import annotations

import uuid
from datetime import datetime

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Boolean, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Enquiry(Base):
    __tablename__ = "cap_intake_enquiries"
    __table_args__ = (UniqueConstraint("channel", "provider_id", name="uq_intake_provider"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    channel: Mapped[str] = mapped_column(String(20))  # phone | sms | mail | form
    sender: Mapped[str] = mapped_column("from", String(320))
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    provider_id: Mapped[str | None] = mapped_column(String(200))
    party_id: Mapped[uuid.UUID | None]
    claimed_email: Mapped[str | None] = mapped_column(String(320))  # rung 0: unverified
    claimed_phone: Mapped[str | None] = mapped_column(String(320))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    classified_as: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(60))
    summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new | classified | opted_out
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class OptOut(Base):
    """Addresses that asked us to stop. Checked by rule before any send or model call."""

    __tablename__ = "cap_intake_optouts"
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), default="")
    at: Mapped[datetime] = mapped_column(default=utcnow)


class SentText(Base):
    """One row per idempotency key: a replayed send returns the first result."""

    __tablename__ = "cap_intake_sent"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    to: Mapped[str] = mapped_column(String(320))
    body: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(default=utcnow)


class Cursor(Base):
    __tablename__ = "cap_intake_cursors"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[datetime]
