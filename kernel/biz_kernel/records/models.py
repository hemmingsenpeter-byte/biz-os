"""Generic records every business needs. Packs extend these via ext_<pack>_* tables only."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, utcnow, uuid7


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid7)


class Party(Base):
    __tablename__ = "parties"
    id: Mapped[uuid.UUID] = _id()
    kind: Mapped[str] = mapped_column(String(20))  # organisation | person
    name: Mapped[str] = mapped_column(Text)
    legal_id_scheme: Mapped[str | None] = mapped_column(String(40))
    legal_id: Mapped[str | None] = mapped_column(String(80))
    domain: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Person(Base):
    __tablename__ = "people"
    id: Mapped[uuid.UUID] = _id()
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parties.id"))
    full_name: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))


class Relationship(Base):
    __tablename__ = "relationships"
    id: Mapped[uuid.UUID] = _id()
    from_party: Mapped[uuid.UUID] = mapped_column(ForeignKey("parties.id"))
    to_party: Mapped[uuid.UUID] = mapped_column(ForeignKey("parties.id"))
    kind: Mapped[str] = mapped_column(String(40))  # client | supplier | subcontractor | partner


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[uuid.UUID] = _id()
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parties.id"))
    label: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    geo: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _id()
    client_party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parties.id"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id"))
    kind: Mapped[str] = mapped_column(String(60))
    stage: Mapped[str] = mapped_column(String(60))
    owner_user_id: Mapped[uuid.UUID | None]
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    opened_at: Mapped[datetime] = mapped_column(default=utcnow)
    closed_at: Mapped[datetime | None]


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = _id()
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(80), default="generic")  # selects declared evidence
    goal_key: Mapped[str | None] = mapped_column(String(120))
    due_at: Mapped[datetime | None]
    assignee: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | done | cancelled
    source_packet_id: Mapped[uuid.UUID | None]


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = _id()
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    kind: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = _id()
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parties.id"))
    channel: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(10))  # in | out
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[uuid.UUID] = _id()
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    author: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))  # voice | typed | system
