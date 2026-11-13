"""`biz audit verify` exits 0 on an intact chain and 1 once a row has been tampered with."""

from __future__ import annotations

from pathlib import Path

from biz_kernel.audit.log import append_only_ddl, record
from biz_kernel.cli.main import app
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
DEMO = str(ROOT / "apps" / "demo")


def test_audit_verify_passes_then_fails_after_tamper(fresh_db: str) -> None:
    assert CliRunner().invoke(app, ["migrate", DEMO, "--url", fresh_db]).exit_code == 0
    res = CliRunner().invoke(app, ["audit", "verify", DEMO, "--url", fresh_db])
    assert res.exit_code == 0 and "intact (empty)" in res.output, res.output

    eng = create_engine(fresh_db)
    with Session(eng) as s, s.begin():
        for i in range(3):
            record(s, "cli-test", "test", i=i)
    res = CliRunner().invoke(app, ["audit", "verify", DEMO, "--url", fresh_db])
    assert res.exit_code == 0 and "intact: 3 rows" in res.output, res.output

    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_log_append_only ON audit_log"))
        conn.execute(text("UPDATE audit_log SET actor = 'someone-else' WHERE seq = 2"))
        conn.exec_driver_sql(append_only_ddl("audit_log"))
    eng.dispose()
    res = CliRunner().invoke(app, ["audit", "verify", DEMO, "--url", fresh_db])
    assert res.exit_code == 1, res.output
    assert "seq 2" in res.output and "broken" in res.output
