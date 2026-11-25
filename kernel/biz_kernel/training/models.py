"""Immutable source snapshots, correction candidates and owner review records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES
from biz_kernel.db import Base, utcnow, uuid7


class DraftSnapshot(Base):
    __tablename__ = "training_draft_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "step_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    run_id: Mapped[str] = mapped_column(String(200), ForeignKey("sop_runs.id"))
    step_id: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(120))
    task: Mapped[str] = mapped_column(String(160))
    prompt_path: Mapped[str] = mapped_column(Text)
    system: Mapped[str] = mapped_column(Text)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    untrusted_text: Mapped[str] = mapped_column(Text)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tier: Mapped[str] = mapped_column(String(10))
    backend: Mapped[str] = mapped_column(String(240))
    configured_model: Mapped[str | None] = mapped_column(String(240))
    model_revision: Mapped[str | None] = mapped_column(String(240))
    system_sha256: Mapped[str] = mapped_column(String(64))
    schema_sha256: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    output_sha256: Mapped[str] = mapped_column(String(64))
    payload_sha256: Mapped[str] = mapped_column(String(64))


class TrainingCandidate(Base):
    __tablename__ = "training_candidates"
    __table_args__ = (UniqueConstraint("correction_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    correction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_corrections.id"))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("training_draft_snapshots.id"))
    rule: Mapped[str] = mapped_column(Text)
    expected_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(120))
    payload_sha256: Mapped[str] = mapped_column(String(64))


class TrainingReview(Base):
    __tablename__ = "training_reviews"
    __table_args__ = (UniqueConstraint("candidate_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_candidates.id"))
    decision: Mapped[str] = mapped_column(String(24))
    by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")


APPEND_ONLY_TABLES.extend(("training_draft_snapshots", "training_candidates", "training_reviews"))
