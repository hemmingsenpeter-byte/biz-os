"""Append-only evidence events: machine proof that something happened."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, event
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


# Database-level backstop for evidence-bound completion: a task can become `done` only
# inside complete_task (which sets a transaction-local token) and only with evidence.
TASK_DONE_GUARD_DDL = """
CREATE OR REPLACE FUNCTION biz_guard_task_done() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'done' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done') THEN
    IF coalesce(current_setting('biz.task_done_ok', true), '') <> NEW.id::text THEN
      RAISE EXCEPTION 'task % can only be completed with evidence (use complete_task)', NEW.id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM evidence_events e
                   WHERE e.task_id = NEW.id OR (e.task_id IS NULL AND e.job_id = NEW.job_id)) THEN
      RAISE EXCEPTION 'task % has no evidence', NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tasks_done_guard ON tasks;
CREATE TRIGGER tasks_done_guard BEFORE INSERT OR UPDATE ON tasks
FOR EACH ROW EXECUTE FUNCTION biz_guard_task_done();
"""


@event.listens_for(Base.metadata, "after_create")
def _install_task_done_guard(target: Any, connection: Any, **_: Any) -> None:
    if "tasks" in target.tables and "evidence_events" in target.tables:
        connection.exec_driver_sql(TASK_DONE_GUARD_DDL.replace("%", "%%"))
