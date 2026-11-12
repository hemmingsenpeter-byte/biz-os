"""Approval tables: staged operations, autonomy levels, suggestions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES
from biz_kernel.db import Base, utcnow, uuid7


class StagedOperation(Base):
    """An action waiting for (or past) approval, with its exact inputs."""

    __tablename__ = "staged_operations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    tool: Mapped[str] = mapped_column(String(160))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    approver: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_version: Mapped[int | None] = mapped_column(Integer)
    approved_sha256: Mapped[str | None] = mapped_column(String(64))
    executed_at: Mapped[datetime | None] = mapped_column()
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decision_type: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")


class AutonomyLevel(Base):
    """Trust level (1-4) per role and decision type, with outcome counters."""

    __tablename__ = "autonomy_levels"
    __table_args__ = (UniqueConstraint("role", "decision_type"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    role: Mapped[str] = mapped_column(String(120))
    decision_type: Mapped[str] = mapped_column(String(120))
    # NULL = counters only: no level was ever set, so the configured default applies.
    level: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    approved_unchanged_streak: Mapped[int] = mapped_column(Integer, default=0)
    good_outcomes: Mapped[int] = mapped_column(Integer, default=0)
    bad_outcomes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Suggestion(Base):
    """A proposed action from the planner or learning loop. Nothing runs on propose."""

    __tablename__ = "suggestions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    source: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(String(160))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    decision_type: Mapped[str | None] = mapped_column(String(120))
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    staged_op_id: Mapped[uuid.UUID | None] = mapped_column()


class Correction(Base):
    """An approver's edit to a proposal: what was drafted, what it became, and why.

    Rows are training data: they become proposed rules and test cases. Never changed.
    """

    __tablename__ = "approval_corrections"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    op_id: Mapped[uuid.UUID] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(120))  # the role whose proposal was edited
    tool: Mapped[str] = mapped_column(String(160))
    decision_type: Mapped[str | None] = mapped_column(String(120))
    from_version: Mapped[int] = mapped_column(Integer)
    to_version: Mapped[int] = mapped_column(Integer)
    before: Mapped[dict[str, Any]] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB)
    by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str | None] = mapped_column(String(10))  # cosmetic | factual | policy


SEVERITIES = ("cosmetic", "factual", "policy")
ERROR_SEVERITIES = ("factual", "policy")  # an untagged edit also counts as an error
APPEND_ONLY_TABLES.append("approval_corrections")
