"""Prove a populated 0023 release can drain, upgrade and recover without duplicate sends.

Runs real DBOS workers from an archived old release and the current checkout, using
disposable local Postgres and fake business providers. No Docker or live credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pgserver
from biz_kernel import migrations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
OLD_RELEASE = "bdf10e6d8f60c0087ee499e4c647ef25d5642468"
CORE_TABLES = (
    "sop_runs",
    "sop_steps",
    "tasks",
    "staged_operations",
    "packets",
    "evidence_events",
    "audit_log",
    "approval_corrections",
)

# Execute with the selected release first on PYTHONPATH, not its installed editable
# counterpart. The old release never imports current ORM columns on its 0023 schema.
BOOTSTRAP = r"""
import json, os, socket, sys
from pathlib import Path
root = Path(os.environ["BIZ_DRILL_ROOT"]).resolve()
for method in ("connect", "connect_ex"):
    original = getattr(socket.socket, method)
    def local(sock, address, original=original):
        if sock.family != socket.AF_UNIX:
            raise RuntimeError("upgrade drill permits only local Unix sockets")
        return original(sock, address)
    setattr(socket.socket, method, local)
import biz_kernel
assert Path(biz_kernel.__file__).resolve().is_relative_to(root), "wrong release imported"
from deploy import smoke_demo as smoke
assert Path(smoke.__file__).resolve().is_relative_to(root), "wrong worker release imported"
from biz_kernel import db, migrations
from sqlalchemy import text
from dbos import DBOS
for name, module in list(sys.modules.items()):
    filename = getattr(module, "__file__", None)
    if name.startswith(("biz_kernel", "biz_cap_", "biz_port_")) and filename:
        assert Path(filename).resolve().is_relative_to(root), "mixed business package releases"
original_init = DBOS.__init__
def offline_init(self, *args, **kwargs):
    kwargs["config"] = {**kwargs["config"], "enable_otlp": False}
    original_init(self, *args, **kwargs)
DBOS.__init__ = offline_init
mode = sys.argv[1]
if mode == "seed":
    migrations.upgrade(os.environ["BIZ_DATABASE_URL"])
    db.configure()
    rt = smoke.runtime(root)
    with db.session_scope() as session:
        request = smoke.DemoRequest(
            sender="upgrade@example.invalid", subject="Upgrade drill", body="Synthetic request"
        )
        session.add(request)
        session.flush()
        packet = rt.bus.publish(
            session, type="service_request.received", from_part="upgrade-drill",
            summary="Upgrade drill", payload={"request_id": str(request.id), "channel": "desk",
                "subject": request.subject, "body": request.body}
        )
        result = {"packet_id": str(packet.id)}
    Path(os.environ["BIZ_DRILL_RESULT"]).write_text(json.dumps(result))
elif mode == "check-startup":
    from biz_kernel.workflows import runner
    from biz_kernel.workflows.versions import assert_startup_compatible
    db.configure()
    smoke.runtime(root)
    with db.session_scope() as session:
        assert_startup_compatible(session, runner.ctx().roles)
else:
    smoke.worker(root, mode)
"""


def isolated_environment(root: Path, settings: dict[str, str]) -> dict[str, str]:
    paths = [root, root / "kernel"]
    paths += sorted((root / "capabilities").glob("*"))
    paths += sorted((root / "ports").glob("*"))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("BIZ_", "DBOS_"))}
    return {
        **env,
        **settings,
        "BIZ_DRILL_ROOT": str(root),
        "PYTHONPATH": os.pathsep.join(map(str, paths)),
    }


def archive_old_release(destination: Path) -> None:
    resolved = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{OLD_RELEASE}^{{commit}}"], text=True
    ).strip()
    if resolved != OLD_RELEASE:
        raise RuntimeError("the pinned old release is unavailable")
    archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", OLD_RELEASE])
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(destination, filter="data")
    if (
        destination / "kernel/biz_kernel/migrations/versions/kernel/0024_training_proposals.py"
    ).exists():
        raise RuntimeError("the old release must precede schema 0024")


def run_child(root: Path, mode: str, env: dict[str, str], log: Path) -> None:
    with log.open("a") as output:
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP, mode],
            cwd=root,
            env=isolated_environment(root, env),
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=90,
        )
    if result.returncode:
        raise RuntimeError(f"{mode} worker failed:\n{log.read_text()[-5000:]}")


def state(engine: Engine, packet_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.id, r.status, r.task_id, t.status AS task_status, "
                    "r.waiting_op_id, o.status AS operation_status FROM sop_runs r "
                    "LEFT JOIN tasks t ON t.id=r.task_id "
                    "LEFT JOIN staged_operations o ON o.id=CAST(r.waiting_op_id AS uuid) "
                    "WHERE r.packet_id=CAST(:packet AS uuid)"
                ),
                {"packet": packet_id},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None


def core_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: connection.scalar(text(f"SELECT count(*) FROM {table}")) for table in CORE_TABLES
        }


def core_history(engine: Engine) -> dict[str, str]:
    """Hash populated history, ignoring the newly added, initially-null workflow pin."""
    with engine.connect() as connection:
        return {
            table: hashlib.sha256(
                json.dumps(
                    list(
                        connection.scalars(
                            text(
                                f"SELECT to_jsonb(row) - 'definition_sha256' FROM {table} AS row "
                                "ORDER BY (to_jsonb(row) - 'definition_sha256')::text"
                            )
                        )
                    ),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for table in CORE_TABLES
        }


def deliveries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_and_kill(root: Path, env: dict[str, str], log: Path, engine: Engine) -> dict[str, Any]:
    with log.open("a") as output:
        child = subprocess.Popen(
            [sys.executable, "-c", BOOTSTRAP, "start"],
            cwd=root,
            env=isolated_environment(root, env),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"worker exited before approval:\n{log.read_text()[-5000:]}")
                current = state(engine, env["BIZ_SMOKE_PACKET"])
                if current and current["status"] == "waiting":
                    assert (
                        current["operation_status"] == "pending"
                        and current["task_status"] == "open"
                    )
                    return current
                time.sleep(0.1)
            raise TimeoutError("worker did not reach a durable approval")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def assert_completed(engine: Engine, packet_id: str) -> None:
    current = state(engine, packet_id)
    assert current and current["status"] == current["task_status"] == "done"
    with engine.connect() as connection:
        kinds = set(
            connection.scalars(
                text("SELECT kind FROM evidence_events WHERE task_id=:task"),
                {"task": current["task_id"]},
            )
        )
        assert {"approval.granted", "packet:message.sent"} <= kinds


def source_digest() -> str:
    paths = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        text=True,
    ).splitlines()
    hashed = hashlib.sha256()
    for name in sorted(set(paths)):
        path = ROOT / name
        if path.is_file():
            hashed.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return hashed.hexdigest()


def drill() -> dict[str, Any]:
    start = time.monotonic()
    initial_source = source_digest()
    with tempfile.TemporaryDirectory(prefix="biz-upgrade-") as name, ExitStack() as stack:
        temporary = Path(name)
        old = temporary / "old-release"
        old.mkdir()
        archive_old_release(old)
        pg = pgserver.get_server(temporary / "postgres", cleanup_mode="delete")
        stack.callback(pg.cleanup)
        pg.psql("CREATE DATABASE application;")
        pg.psql("CREATE DATABASE workflow;")
        app_url = pg.get_uri("application").replace("postgresql://", "postgresql+psycopg://", 1)
        system_url = pg.get_uri("workflow").replace("postgresql://", "postgresql+psycopg://", 1)
        engine = create_engine(app_url)
        stack.callback(engine.dispose)
        # Parent connections, like worker connections, are Unix-socket-only.
        for method in ("connect", "connect_ex"):
            original = getattr(socket.socket, method)

            def local(sock: socket.socket, address: Any, original: Any = original) -> Any:
                if sock.family != socket.AF_UNIX:
                    raise RuntimeError("upgrade drill permits only local Unix sockets")
                return original(sock, address)

            stack.enter_context(patch.object(socket.socket, method, local))
        sends, result, log = (
            temporary / filename for filename in ("sends.jsonl", "seed.json", "worker.log")
        )
        env = {
            "BIZ_DATABASE_URL": app_url,
            "BIZ_DBOS_SYSTEM_URL": system_url,
            "BIZ_SMOKE_SENDS": str(sends),
            "BIZ_DRILL_RESULT": str(result),
        }
        run_child(old, "seed", env, log)
        legacy_packet = json.loads(result.read_text())["packet_id"]
        env["BIZ_SMOKE_PACKET"] = legacy_packet
        pending = wait_and_kill(old, env, log, engine)
        assert deliveries(sends) == []
        before = core_counts(engine)
        history = core_history(engine)
        try:
            migrations.upgrade(app_url)
        except RuntimeError as exc:
            if "drain on their original release" not in str(exc):
                raise
        else:
            raise AssertionError("legacy unfinished work did not block migration")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0023"
        assert "definition_sha256" not in {
            c["name"] for c in inspect(engine).get_columns("sop_runs")
        }
        assert "training_draft_snapshots" not in inspect(engine).get_table_names()
        assert core_counts(engine) == before and state(engine, legacy_packet) == pending
        assert core_history(engine) == history
        for mode in ("resume", "replay"):
            run_child(old, mode, env, log)
        assert_completed(engine, legacy_packet)
        assert len(deliveries(sends)) == 1
        before = core_counts(engine)
        history = core_history(engine)
        migrations.upgrade(app_url)
        with engine.connect() as connection:
            final_schema = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert core_counts(engine) == before
        assert core_history(engine) == history
        run_child(ROOT, "check-startup", env, log)
        run_child(ROOT, "replay", env, log)
        assert_completed(engine, legacy_packet)
        assert len(deliveries(sends)) == 1
        assert core_history(engine) == history
        run_child(ROOT, "seed", env, log)
        current_packet = json.loads(result.read_text())["packet_id"]
        env["BIZ_SMOKE_PACKET"] = current_packet
        wait_and_kill(ROOT, env, log, engine)
        assert len(deliveries(sends)) == 1
        run_child(ROOT, "check-startup", env, log)
        for mode in ("resume", "replay"):
            run_child(ROOT, mode, env, log)
        assert_completed(engine, current_packet)
        sent = deliveries(sends)
        assert len(sent) == len({row["key"] for row in sent}) == 2
        with engine.connect() as connection:
            pins = connection.execute(
                text(
                    "SELECT packet_id, definition_sha256 FROM sop_runs "
                    "WHERE packet_id IN (CAST(:legacy AS uuid), CAST(:current AS uuid))"
                ),
                {"legacy": legacy_packet, "current": current_packet},
            ).all()
            by_packet = {str(packet): pin for packet, pin in pins}
            assert by_packet[legacy_packet] is None and len(by_packet[current_packet]) == 64
            snapshots = connection.scalar(text("SELECT count(*) FROM training_draft_snapshots"))
            assert snapshots == 1
        assert source_digest() == initial_source, (
            "source changed during the upgrade drill; rerun it"
        )
        return {
            "status": "PASS",
            "old_release": OLD_RELEASE,
            "old_schema": "0023",
            "final_schema": final_schema,
            "new_source_sha256": initial_source,
            "new_release_head": subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
            ).strip(),
            "upgrade_refused_before_drain": True,
            "refusal_preserved_legacy_data": True,
            "legacy_drained_on_original_release": True,
            "legacy_history_preserved": True,
            "legacy_replayed_without_new_send": True,
            "current_recovery_and_replay": True,
            "fake_send_invocations": len(sent),
            "current_draft_snapshots": snapshots,
            "external_network": "blocked",
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "scope": (
                "disposable local PostgreSQL, real DBOS and fake providers; "
                "not appliance acceptance"
            ),
        }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".upgrade-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(evidence, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="optional JSON evidence file")
    args = parser.parse_args()
    if args.out:
        args.out = args.out.expanduser().resolve()
        if any((parent / ".git").exists() for parent in args.out.parents):
            parser.error("--out must be outside Git working trees")
    if args.out:
        write_evidence(args.out, {"status": "RUNNING", "old_release": OLD_RELEASE})
    try:
        evidence = drill()
    except (Exception, KeyboardInterrupt) as exc:
        if args.out:
            write_evidence(
                args.out,
                {
                    "status": "UNVERIFIED",
                    "old_release": OLD_RELEASE,
                    "error": "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc),
                },
            )
        raise
    content = json.dumps(evidence, indent=2) + "\n"
    if args.out:
        write_evidence(args.out, evidence)
    print(content, end="")


if __name__ == "__main__":
    main()
