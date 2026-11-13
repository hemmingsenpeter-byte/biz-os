"""Worker process: migrations, runtime, DBOS launch, then the heartbeat loop.

Env: BIZ_DATABASE_URL, BIZ_DBOS_SYSTEM_URL (default: BIZ_DATABASE_URL with db name + "_dbos"),
BIZ_APP_DIR, BIZ_REPO_ROOT, BIZ_TICK_SECONDS (default 60).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from biz_kernel import db, migrations
from biz_kernel.control import service as control
from biz_kernel.health import service as health

log = logging.getLogger("biz.worker")


def dbos_system_url(database_url: str) -> str:
    """Same server as the app database, database name + '_dbos'."""
    url = make_url(database_url)
    return url.set(database=f"{url.database}_dbos").render_as_string(hide_password=False)


def ensure_database(url: str) -> None:
    """Create the database behind `url` if it does not exist (needs CREATEDB)."""
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("select 1 from pg_database where datname = :n"), {"n": u.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        admin.dispose()


def run_once(rt: Any, now: datetime | None = None) -> list[str]:
    """One iteration: heartbeat tick, missed-schedule check, audit seal. Returns woken roles."""
    from biz_kernel.audit import log as audit

    now = now or db.utcnow()
    with db.session_scope() as s:
        paused = control.is_paused(s)  # read before the tick: a resume during it changes it
    woken = rt.tick(now)
    with db.session_scope() as s:
        if not paused and not control.is_paused(s):  # paused heartbeats are not missed runs
            health.check_missed(s, now)
    with db.session_scope() as s:
        audit.seal(s)  # hash-chain the audit rows written since the last heartbeat
    return woken


def main() -> None:
    from dbos import DBOS

    from biz_kernel.cli.main import find_repo_root
    from biz_kernel.runtime import build_runtime

    logging.basicConfig(level=logging.INFO)
    app_dir = Path(os.environ["BIZ_APP_DIR"])
    repo_root = Path(os.environ["BIZ_REPO_ROOT"]) if "BIZ_REPO_ROOT" in os.environ else None
    repo_root = repo_root or find_repo_root(app_dir)
    url = os.environ.get("BIZ_DATABASE_URL") or db.database_url()
    sys_url = os.environ.get("BIZ_DBOS_SYSTEM_URL") or dbos_system_url(url)
    tick = float(os.environ.get("BIZ_TICK_SECONDS", "60"))

    db.configure(url)
    migrations.upgrade(url)
    ensure_database(sys_url)
    rt = build_runtime(app_dir, repo_root)
    DBOS(
        config={
            "name": f"biz-{rt.manifest.name}",
            "system_database_url": sys_url,
        }
    )
    DBOS.launch()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker up: %s, tick %ss", rt.manifest.name, tick)
    try:
        while not stop.is_set():
            try:
                run_once(rt)
            except Exception:  # keep the loop alive; the missed-run check will flag silence
                log.exception("tick failed")
            stop.wait(tick)
    finally:
        DBOS.destroy()
        log.info("worker stopped")


if __name__ == "__main__":
    main()
