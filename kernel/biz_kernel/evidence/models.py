"""Append-only evidence events: machine proof that something happened."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES
from biz_kernel.db import Base, utcnow, uuid7


class EvidenceEvent(Base):
    __tablename__ = "evidence_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(120))  # packet:<type> | approval.granted | ...
    ref: Mapped[str | None] = mapped_column(String(240))
    job_id: Mapped[uuid.UUID | None]
    task_id: Mapped[uuid.UUID | None]
    at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_evidence_events_task", "task_id", "kind"),)


if "evidence_events" not in APPEND_ONLY_TABLES:
    APPEND_ONLY_TABLES.append("evidence_events")
