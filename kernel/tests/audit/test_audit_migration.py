"""Migration 0021 seals the rows already in audit_log, in (at, id) order; verify is clean."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from biz_kernel import migrations
from biz_kernel.audit import log
from biz_kernel.audit.log import AuditEntry, record, seal, verify
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


def test_backfill_chains_existing_rows_and_verifies_clean(fresh_db: str) -> None:
    migrations.upgrade(fresh_db, "0020")
    eng = create_engine(fresh_db)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Inserted out of time order; the backfill must chain by (at, id).
    rows = [
        (3, {"amount": 12.5, "nested": {"b": [1, 2.0, None], "a": "é"}}),
        (1, {}),
        (2, {"big": 1e20}),
        (2, {"same_at": True}),
    ]
    with eng.begin() as conn:
        for minutes, detail in rows:
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, at, actor, kind, subject, detail) "
                    "VALUES (:id, :at, 'role', 'test', NULL, CAST(:d AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "at": base + timedelta(minutes=minutes),
                    "d": json.dumps(detail),
                },
            )

    migrations.upgrade(fresh_db)

    with Session(eng) as s:
        chained = list(s.scalars(select(AuditEntry).order_by(AuditEntry.seq)))
        assert [e.seq for e in chained] == [1, 2, 3, 4]
        assert [(e.at, str(e.id)) for e in chained] == sorted((e.at, str(e.id)) for e in chained)
        assert verify(s) == []
    with Session(eng) as s, s.begin():
        assert record(s, "role", "after").seq is None  # unsealed until the next seal
    with Session(eng) as s:
        assert seal(s) == 1
        tail = log.head(s)
        assert tail is not None and tail[0] == 5
        assert verify(s) == []

    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor = 'x'"))

    migrations.downgrade(fresh_db, "0020")
    cols = {c["name"] for c in inspect(eng).get_columns("audit_log")}
    assert not cols & {"seq", "prev_hash", "hash"}
    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor = 'x'"))
    eng.dispose()
