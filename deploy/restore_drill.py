"""Run the backup and restore scripts against disposable Postgres and local restic.

Requires a local Docker Compose v2 engine, the pgvector image and restic on PATH.
Creates, destroys and recreates isolated Compose projects and temporary files.
Run with: uv run python -m deploy.restore_drill
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from biz_cap_demo_desk.models import DemoRequest
from biz_kernel import db, migrations
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.audit import log as audit
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.records.models import Task
from biz_kernel.workflows.models import SopRun
from biz_kernel.workflows.runner import run_id_for
from biz_kernel.workflows.worker import dbos_system_url, ensure_database
from biz_port_storage_local import LocalStorage
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from deploy import smoke_demo

COMPOSE = """\
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: biz
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
      POSTGRES_DB: postgres
    ports:
      - "127.0.0.1::5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U biz -d postgres"]
      interval: 1s
      timeout: 3s
      retries: 60
    volumes:
      - pgdata:/var/lib/postgresql/data
  docuseal:
    image: busybox:1.37.0
    volumes:
      - docuseal:/data/docuseal
  biz-api: &objects
    image: busybox:1.37.0
    volumes:
      - type: bind
        source: ${BIZ_STORAGE_HOST_ROOT:?}
        target: /var/lib/biz-os/objects
  biz-worker: *objects
volumes:
  pgdata:
  docuseal:
"""

DOCUMENT = b"Offline restore drill object\n"
SIGNATURE_FILE = "offline signed file"
SIGNATURE_PATH = "/data/docuseal/drill/signed.txt"


def run(command: list[str], env: dict[str, str], timeout: int = 180) -> str:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        output = (result.stdout + result.stderr)[-4000:]
        raise RuntimeError(f"{command[0]} exited with status {result.returncode}:\n{output}")
    return result.stdout.strip()


class Project:
    def __init__(self, env: dict[str, str], name: str, tmp: Path) -> None:
        self.env = {**env, "COMPOSE_PROJECT_NAME": name}
        config = tmp / f"{name}.env"
        config.write_text(f"POSTGRES_PASSWORD={env['POSTGRES_PASSWORD']}\n")
        config.chmod(0o600)
        self.env["BIZ_ENV_FILE"] = str(config)
        self.command = [
            "docker",
            "compose",
            "-f",
            self.env["BIZ_COMPOSE_FILE"],
            "--env-file",
            str(config),
            "--project-name",
            name,
        ]

    def compose(self, *args: str) -> str:
        return run([*self.command, *args], self.env)

    def start(self) -> URL:
        try:
            self.compose("up", "-d", "--wait", "--wait-timeout", "75", "postgres")
        except RuntimeError:
            print(self.compose("logs", "--no-color", "--tail", "30", "postgres"), file=sys.stderr)
            raise
        self.compose("create", "docuseal")
        port = self.compose("port", "postgres", "5432").split(":")[-1]
        return URL.create(
            "postgresql+psycopg",
            username="biz",
            password=self.env["POSTGRES_PASSWORD"],
            host="127.0.0.1",
            port=int(port),
            database="biz",
        )

    def docuseal_volume(self, script: str, *args: str) -> str:
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "sh",
            "docuseal",
            "-ec",
            script,
            "restore-drill",
            *args,
        )


@contextmanager
def project(env: dict[str, str], purpose: str, tmp: Path) -> Iterator[Project]:
    instance = Project(env, f"biz-drill-{purpose}-{uuid.uuid4().hex[:12]}", tmp)
    try:
        yield instance
    finally:
        instance.compose("down", "--volumes", "--remove-orphans")


def seed_docuseal(source: Project, url: URL) -> None:
    docuseal_url = url.set(database="docuseal")
    ensure_database(docuseal_url.render_as_string(hide_password=False))
    engine = create_engine(docuseal_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE drill_signatures (name text NOT NULL)"))
            connection.execute(
                text("INSERT INTO drill_signatures VALUES (:name)"), {"name": SIGNATURE_PATH}
            )
    finally:
        engine.dispose()
    source.docuseal_volume(
        'mkdir -p "$(dirname "$1")"; printf "%s" "$2" > "$1"', SIGNATURE_PATH, SIGNATURE_FILE
    )


def verify_docuseal(target: Project, url: URL) -> None:
    engine = create_engine(url.set(database="docuseal"))
    try:
        with engine.connect() as connection:
            filename = connection.execute(text("SELECT name FROM drill_signatures")).scalar_one()
    finally:
        engine.dispose()
    assert filename == SIGNATURE_PATH
    assert target.docuseal_volume('cat "$1"', filename) == SIGNATURE_FILE


def seed_workflow(root: Path, storage: LocalStorage) -> tuple[uuid.UUID, uuid.UUID]:
    document = storage.put(DOCUMENT, "restore-drill.txt", "text/plain")
    rt = smoke_demo.runtime(root)
    with db.session_scope() as session:
        request = DemoRequest(
            sender="smoke@example.invalid", subject="Restore drill", body="An offline enquiry"
        )
        session.add(request)
        session.flush()
        packet = rt.bus.publish(
            session,
            type="service_request.received",
            from_part="restore_drill",
            summary="Restore drill",
            payload={
                "request_id": str(request.id),
                "channel": "desk",
                "subject": request.subject,
                "body": request.body,
            },
        )
        marker = audit.record(
            session, "restore_drill", "stored_document", str(request.id), uri=document.uri
        )
        return packet.id, marker.id


def pending_approval(session: Session, packet_id: uuid.UUID) -> StagedOperation | None:
    workflow = session.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
    if workflow is None or workflow.status != "waiting":
        return None
    task = session.get(Task, workflow.task_id)
    assert task is not None and task.status == "open"
    assert workflow.waiting_op_id is not None
    op = session.get(StagedOperation, uuid.UUID(workflow.waiting_op_id))
    assert op is not None and op.status == "pending"
    assert op.tool == "demo_desk.send_reply"
    return op


def start_and_crash(
    command: list[str], env: dict[str, str], packet_id: uuid.UUID, log_path: Path
) -> None:
    with log_path.open("w+") as log:
        worker = subprocess.Popen(
            [*command, "--worker", "start"], env=env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if worker.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(f"worker exited before approval:\n{log.read()[-3000:]}")
                with db.session_scope() as session:
                    if pending_approval(session, packet_id) is not None:
                        return
                time.sleep(0.1)
            raise TimeoutError("worker did not reach approval within 60 seconds")
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=10)


def worker_env(env: dict[str, str], url: str, packet_id: uuid.UUID, sends: Path) -> dict[str, str]:
    return {
        **env,
        "BIZ_DATABASE_URL": url,
        "BIZ_DBOS_SYSTEM_URL": dbos_system_url(url),
        "BIZ_SMOKE_PACKET": str(packet_id),
        "BIZ_SMOKE_SENDS": str(sends),
    }


def dbos_status(url: str, packet_id: uuid.UUID) -> str:
    engine = create_engine(dbos_system_url(url))
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :id"),
                    {"id": run_id_for("demo_clerk", "handle_request", packet_id)},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def verify_recovery(
    command: list[str],
    env: dict[str, str],
    packet_id: uuid.UUID,
    approval_id: uuid.UUID,
    inputs: dict[str, Any],
    sends: Path,
) -> None:
    run([*command, "--worker", "resume"], env, timeout=90)
    run([*command, "--worker", "replay"], env, timeout=90)
    deliveries = [json.loads(line) for line in sends.read_text().splitlines()]
    assert deliveries == [
        {
            "key": inputs["idempotency_key"],
            "to": [inputs["to"]],
            "subject": inputs["subject"],
            "body": inputs["body"],
        }
    ]
    assert dbos_status(env["BIZ_DATABASE_URL"], packet_id) == "SUCCESS"
    with db.session_scope() as session:
        workflow = session.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
        assert workflow is not None and workflow.status == "done"
        task = session.get(Task, workflow.task_id)
        assert task is not None and task.status == "done"
        kinds = set(
            session.scalars(select(EvidenceEvent.kind).where(EvidenceEvent.task_id == task.id))
        )
        assert {"approval.granted", "packet:message.sent"} <= kinds
        op = session.get(StagedOperation, approval_id)
        assert op is not None and op.status == "executed"


def drill(root: Path, tmp: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    # Leave the user's Docker CLI configuration available but use only temp projects and
    # a new local repository; do not inherit provider or backup credentials.
    compose_file = tmp / "compose.yml"
    compose_file.write_text(COMPOSE)
    objects = tmp / "objects"
    objects.mkdir(mode=0o700)
    env.update(
        BIZ_BACKUP_HELPER_IMAGE="busybox:1.37.0",
        BIZ_COMPOSE_FILE=str(compose_file),
        BIZ_DUMP_DIR=str(tmp / "dumps"),
        BIZ_STORAGE_HOST_ROOT=str(objects),  # the mount the backup scripts verify
        BIZ_STORAGE_ROOT=str(objects),  # in-process application path
        POSTGRES_PASSWORD=secrets.token_hex(24),
        RESTIC_REPOSITORY=str(tmp / "restic"),
        RESTIC_PASSWORD=secrets.token_hex(32),
        RESTIC_CACHE_DIR=str(tmp / "restic-cache"),
    )
    run(["restic", "init"], env)
    sends = tmp / "sends.jsonl"  # observer external to both application environments
    command = [sys.executable, "-m", "deploy.smoke_demo", "--repo-root", str(root)]

    with project(env, "source", tmp) as source:
        url = source.start()
        app_url = url.render_as_string(hide_password=False)
        ensure_database(app_url)
        ensure_database(dbos_system_url(app_url))
        seed_docuseal(source, url)
        engine = db.configure(app_url)
        try:
            migrations.upgrade(app_url)
            packet_id, marker_id = seed_workflow(root, LocalStorage(objects))
            start_and_crash(
                command, worker_env(env, app_url, packet_id, sends), packet_id, tmp / "worker.log"
            )
            assert not sends.exists(), "source fake mail was invoked before approval"
            assert dbos_status(app_url, packet_id) == "PENDING"
            with db.session_scope() as session:
                op = pending_approval(session, packet_id)
                assert op is not None
                approval_id, inputs, inputs_hash = op.id, op.inputs, op.inputs_sha256
                audit.seal(session)
                expected_head = audit.head(session)
                assert expected_head is not None and not audit.verify(session)
        finally:
            engine.dispose()

        checkpoint = time.monotonic()
        print("PASS: source has a sealed audit chain, pending approval and stopped workflow")
        output = run(["bash", str(root / "deploy/backups/backup.sh")], source.env, timeout=300)
        match = re.search(r"^backup ok ([0-9a-f]{64})$", output, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"backup did not report a verified snapshot ID:\n{output[-2000:]}")
        snapshot = match.group(1)
        loss = time.monotonic()

    # Source containers and volumes are now gone. Preserve the logical storage path
    # used in persisted file: URIs while making it physically empty for the target.
    shutil.rmtree(objects)
    objects.mkdir(mode=0o700)
    with project(env, "target", tmp) as target:
        url = target.start()
        run(["bash", str(root / "deploy/backups/restore.sh"), snapshot], target.env, timeout=300)
        verify_docuseal(target, url)
        app_url = url.render_as_string(hide_password=False)
        engine = db.configure(app_url)
        try:
            assert dbos_status(app_url, packet_id) == "PENDING"
            with db.session_scope() as session:
                restored = pending_approval(session, packet_id)
                assert restored is not None and restored.id == approval_id
                assert restored.inputs == inputs and restored.inputs_sha256 == inputs_hash
                assert audit.head(session) == expected_head and not audit.verify(session)
                marker = session.get(audit.AuditEntry, marker_id)
                assert marker is not None
                assert LocalStorage(objects).get(marker.detail["uri"]) == DOCUMENT
            assert not sends.exists(), "fake mail was invoked before restored approval"
            print("PASS: clean target restored three databases, both file stores and audit head")
            verify_recovery(
                command,
                worker_env(env, app_url, packet_id, sends),
                packet_id,
                approval_id,
                inputs,
                sends,
            )
            recovered = time.monotonic()
        finally:
            engine.dispose()

    print("PASS: restored workflow completed with evidence and replay invoked fake mail once total")
    print(
        f"Drill RPO (age of last quiesced checkpoint at simulated loss): {loss - checkpoint:.2f}s"
    )
    print(
        f"Drill RTO (simulated loss through verified recovery and replay): {recovered - loss:.2f}s"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    if not __debug__:
        parser.error("the restore drill requires Python assertions to be enabled")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="biz-restore-drill-") as temporary:
        drill(args.repo_root.resolve(), Path(temporary).resolve())
