"""The kernel Alembic branch builds exactly Base.metadata, with append-only triggers."""

from __future__ import annotations

import importlib

import pytest
from biz_kernel import migrations
from biz_kernel.audit import log
from biz_kernel.db import Base
from biz_kernel.models_registry import import_all_models
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


def _tables(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        return set(inspect(eng).get_table_names()) - {"alembic_version"}
    finally:
        eng.dispose()


def _triggers(engine: Engine) -> dict[str, tuple[str, str, str]]:
    with engine.connect() as conn:
        return {
            name: (table, enabled, definition)
            for name, table, enabled, definition in conn.execute(
                text(
                    "select tgname, tgrelid::regclass::text, tgenabled::text, "
                    "pg_get_triggerdef(oid) from pg_trigger where not tgisinternal"
                )
            )
        }


def test_upgrade_matches_metadata_and_downgrade_empties(fresh_db: str) -> None:
    import_all_models()
    importlib.import_module("biz_cap_demo_desk.models")
    migrations.upgrade(fresh_db)
    assert _tables(fresh_db) == set(Base.metadata.tables)

    eng = create_engine(fresh_db)
    triggers = _triggers(eng)
    assert set(triggers) == {
        f"{table}_{suffix}"
        for table in log.APPEND_ONLY_TABLES
        for suffix in ("append_only", "reject_truncate")
    } | {"tasks_done_guard"}
    for table in log.APPEND_ONLY_TABLES:
        owner, enabled, definition = triggers[f"{table}_reject_truncate"]
        assert (owner, enabled) == (table, "O")
        assert "BEFORE TRUNCATE" in definition
        assert "FOR EACH STATEMENT" in definition
        assert "biz_reject_mutation()" in definition
        with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table} CASCADE"))

    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(
            text(
                "insert into audit_log (id, at, actor, kind, detail) values "
                "(gen_random_uuid(), now(), 'a', 'k', '{}')"
            )
        )
        conn.execute(text("update audit_log set actor = 'b'"))

    migrations.downgrade(fresh_db, "0022")
    remaining = set(log.APPEND_ONLY_TABLES) & _tables(fresh_db)
    assert set(_triggers(eng)) == {f"{table}_append_only" for table in remaining} | {
        "tasks_done_guard"
    }
    eng.dispose()

    migrations.downgrade(fresh_db, allow_data_loss=True)
    assert _tables(fresh_db) == set()
