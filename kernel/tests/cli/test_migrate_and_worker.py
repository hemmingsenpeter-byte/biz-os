"""`biz migrate` upgrades a database; the worker loop body runs one tick."""

from __future__ import annotations

from pathlib import Path

from biz_kernel.cli.main import app
from biz_kernel.workflows.worker import dbos_system_url, run_once
from sqlalchemy import create_engine, inspect
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def test_migrate_upgrades_to_head(fresh_db: str) -> None:
    res = CliRunner().invoke(app, ["migrate", str(ROOT / "apps" / "demo"), "--url", fresh_db])
    assert res.exit_code == 0, res.output
    assert "audit_log" in inspect(create_engine(fresh_db)).get_table_names()


def test_dbos_system_url_appends_suffix() -> None:
    assert dbos_system_url("postgresql+psycopg://u:p@h:5432/biz").endswith("/biz_dbos")


def test_run_once_ticks_then_checks_missed(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from biz_kernel import db

    calls: list[object] = []

    class Rt:
        def tick(self, now):  # type: ignore[no-untyped-def]
            calls.append(("tick", now))
            return ["role"]

    class Scope:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return session

        def __exit__(self, *a):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr(db, "session_scope", lambda: Scope())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert run_once(Rt(), now) == ["role"]
    assert calls == [("tick", now)]
