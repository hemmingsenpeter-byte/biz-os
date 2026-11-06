"""Killing the worker mid-SOP and restarting resumes from the last completed step, no duplicates."""

import os
import subprocess
import sys
from pathlib import Path

from biz_kernel import db
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.orm import Session

WORKER = Path(__file__).parents[2] / "kernel" / "tests" / "workflows" / "worker.py"


def _worker(url: str, sys_url: str, mode: str, packet_id: str, marker: Path):
    return subprocess.run(
        [sys.executable, str(WORKER), url, sys_url, mode, packet_id, str(marker)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ},
    )


def test_workflow_resume(fresh_db: str, tmp_path: Path) -> None:
    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    with Session(eng) as s:
        p = Packet(type="demo.ping", schema_version=1, from_part="test", payload={})
        s.add(p)
        s.commit()
        packet_id = str(p.id)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)
    marker = tmp_path / "crashed"

    first = _worker(fresh_db, sys_url, "crash", packet_id, marker)
    assert first.returncode == 17, first.stderr[-2000:]
    assert marker.exists()

    second = _worker(fresh_db, sys_url, "resume", packet_id, marker)
    assert second.returncode == 0, second.stderr[-2000:]
    assert "RESULT done notes=1" in second.stdout, second.stdout + second.stderr[-2000:]

    with Session(eng) as s:
        done = s.scalar(select(func.count()).select_from(Packet).where(Packet.type == "demo.done"))
        assert done == 1
        assert s.execute(text("select status from sop_runs")).scalar() == "done"
    eng.dispose()
