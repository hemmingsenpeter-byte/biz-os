"""The kernel Alembic branch builds exactly Base.metadata, with append-only triggers."""

from __future__ import annotations

import importlib

import pytest
from biz_kernel import migrations
from biz_kernel.audit import log
from biz_kernel.db import Base
from biz_kernel.models_registry import import_all_models
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError


def _tables(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        return set(inspect(eng).get_table_names()) - {"alembic_version"}
    finally:
        eng.dispose()


def test_upgrade_matches_metadata_and_downgrade_empties(fresh_db: str) -> None:
    import_all_models()
    importlib.import_module("biz_cap_demo_desk.models")
    migrations.upgrade(fresh_db)
    assert _tables(fresh_db) == set(Base.metadata.tables)

    eng = create_engine(fresh_db)
    with eng.connect() as conn:
        trig = {
            r[0] for r in conn.execute(text("select tgname from pg_trigger where not tgisinternal"))
        }
    assert trig == {f"{t}_append_only" for t in log.APPEND_ONLY_TABLES}

    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(
            text(
                "insert into audit_log (id, at, actor, kind, detail) values "
                "(gen_random_uuid(), now(), 'a', 'k', '{}')"
            )
        )
        conn.execute(text("update audit_log set actor = 'b'"))
    eng.dispose()

    migrations.downgrade(fresh_db)
    assert _tables(fresh_db) == set()
