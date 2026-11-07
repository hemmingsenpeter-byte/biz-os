"""SOP run bookkeeping. sop_steps is the step ledger that makes every step safe to resume."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, utcnow


class SopRun(Base):
    __tablename__ = "sop_runs"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # DBOS workflow id
    role: Mapped[str] = mapped_column(String(80))
    sop: Mapped[str] = mapped_column(String(120))
    packet_id: Mapped[uuid.UUID | None]
    job_id: Mapped[uuid.UUID | None]
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    goal_key: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="running")
    # running | waiting | budget_stopped | done | rejected | failed
    waiting_op_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class SopStep(Base):
    __tablename__ = "sop_steps"
    run_id: Mapped[str] = mapped_column(ForeignKey("sop_runs.id"), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    output: Mapped[Any] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
