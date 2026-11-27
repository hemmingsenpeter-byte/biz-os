"""Immutable source snapshots, correction candidates and owner review records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
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


class TrainingVersion(Base):
    __tablename__ = "training_versions"
    __table_args__ = (UniqueConstraint("candidate_id", "number"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_candidates.id"))
    candidate_sha256: Mapped[str] = mapped_column(String(64))
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("training_versions.id"))
    parent_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("training_draft_snapshots.id"))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    rule: Mapped[str] = mapped_column(Text)
    system: Mapped[str] = mapped_column(Text)
    expected_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(120))
    payload_sha256: Mapped[str] = mapped_column(String(64))


class TrainingVersionReview(Base):
    __tablename__ = "training_version_reviews"
    __table_args__ = (UniqueConstraint("version_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_versions.id"))
    version_sha256: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(24))
    by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))


class TrainingCohort(Base):
    __tablename__ = "training_cohorts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    name: Mapped[str] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(String(160))
    schema_sha256: Mapped[str] = mapped_column(String(64))
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(120))
    payload_sha256: Mapped[str] = mapped_column(String(64))


class TrainingCohortReview(Base):
    __tablename__ = "training_cohort_reviews"
    __table_args__ = (UniqueConstraint("cohort_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    cohort_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_cohorts.id"))
    cohort_sha256: Mapped[str] = mapped_column(String(64))
    by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))


class ComparisonJob(Base):
    __tablename__ = "training_comparisons"
    __table_args__ = (UniqueConstraint("created_by", "idempotency_key"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_versions.id"))
    cohort_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_cohorts.id"))
    version_review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_version_reviews.id"))
    cohort_review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_cohort_reviews.id"))
    created_by: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    contract: Mapped[dict[str, Any]] = mapped_column(JSONB)
    request_sha256: Mapped[str] = mapped_column(String(64))
    payload_sha256: Mapped[str] = mapped_column(String(64))


class ComparisonEvent(Base):
    __tablename__ = "training_comparison_events"
    __table_args__ = (UniqueConstraint("job_id", "event_key"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_comparisons.id"))
    kind: Mapped[str] = mapped_column(String(40))
    event_key: Mapped[str] = mapped_column(String(240))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))


APPEND_ONLY_TABLES.extend(
    (
        "training_versions",
        "training_version_reviews",
        "training_cohorts",
        "training_cohort_reviews",
        "training_comparisons",
        "training_comparison_events",
    )
)
