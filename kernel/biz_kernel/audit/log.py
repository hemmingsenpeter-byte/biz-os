"""Append-only audit log. A trigger rejects UPDATE and DELETE (see APPEND_ONLY_DDL)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from biz_kernel.db import Base, utcnow, uuid7


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(60))  # tool_call | packet | approval | policy | ...
    subject: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# Tables that must never be updated or deleted. Other modules append to this list.
APPEND_ONLY_TABLES: list[str] = ["audit_log"]

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION biz_reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'table %% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def append_only_ddl(table: str) -> str:
    return (
        f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION biz_reject_mutation();"
    )


@event.listens_for(Base.metadata, "after_create")
def _install_append_only(target: Any, connection: Any, **_: Any) -> None:
    connection.exec_driver_sql(APPEND_ONLY_FN)
    for table in APPEND_ONLY_TABLES:
        if table in target.tables:
            connection.exec_driver_sql(append_only_ddl(table))


def record(
    session: Session, actor: str, kind: str, subject: str | None = None, **detail: Any
) -> AuditEntry:
    entry = AuditEntry(actor=actor, kind=kind, subject=subject, detail=_jsonable(detail))
    session.add(entry)
    session.flush()
    return entry


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value
