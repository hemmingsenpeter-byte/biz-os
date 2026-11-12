"""Offline approval/recovery smoke check using disposable Postgres databases.

Run with the packaged app's Python and BIZ_DATABASE_URL set to a Postgres server
where the connected role can create databases. Only apps/demo and fake ports run.
The two randomly named databases are dropped on exit; the original DB is untouched.
Replay checks that the fake adapter is invoked once across worker restarts; it does
not replace the provider-ledger and uncertain-send tests in the main test suite.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from biz_cap_demo_desk.models import DemoRequest
from biz_cap_documents.render import html_to_pdf
from biz_kernel import db, migrations
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.loader import load_composition
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Task
from biz_kernel.runtime import build_runtime
from biz_kernel.workflows import runner
from biz_kernel.workflows.gate import decide
from biz_kernel.workflows.models import SopRun
from biz_port_fakes import FakeMail, FakeModel
from dbos import DBOS
from pypdf import PdfReader
from sqlalchemy import create_engine, func, make_url, select, text

REPLY = {"subject": "Re: Smoke check", "body": "A deterministic offline reply.", "confidence": 0.9}


class ObservedMail(FakeMail):
    """Record every send invocation across worker restarts, then use the normal fake."""

    async def send(self, msg: Any, idempotency_key: str) -> Any:
        with Path(os.environ["BIZ_SMOKE_SENDS"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {"key": idempotency_key, "to": msg.to, "subject": msg.subject, "body": msg.body}
                )
                + "\n"
            )
        return await super().send(msg, idempotency_key)


def runtime(root: Path) -> Any:
    manifest = load_composition(root / "apps/demo", root)
    if any(not port.startswith("fake_") for port in manifest.ports.values()):
        raise RuntimeError("smoke check requires an entirely fake demo composition")
    rt = build_runtime(
        root / "apps/demo",
        root,
        ports={"mail": ObservedMail(), "model_local": FakeModel([REPLY])},
        approval_poll_seconds=1,
    )
    return rt


def worker(root: Path, mode: str) -> None:
    db.configure()
    rt = runtime(root)
    packet_id = uuid.UUID(os.environ["BIZ_SMOKE_PACKET"])
    DBOS(
        config={
            "name": "biz-demo-smoke",
            "system_database_url": os.environ["BIZ_DBOS_SYSTEM_URL"],
            "application_version": "demo-smoke-v1",
            "executor_id": "demo-smoke",
        }
    )
    DBOS.launch()
    try:
        if mode == "start":
            rt.tick()
        elif mode == "resume":
            with db.session_scope() as session:
                run = session.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
                assert run is not None and run.waiting_op_id is not None
                op = session.get(StagedOperation, uuid.UUID(run.waiting_op_id))
                assert op is not None and op.status == "pending"
                op_id, approver, version = op.id, op.approver, op.version
            decide(op_id, approver, approve=True, version=version)
        else:
            runner.start_for_packet("demo_clerk", packet_id, "service_request.received")
        workflow = runner.run_id_for("demo_clerk", "handle_request", packet_id)
        assert DBOS.retrieve_workflow(workflow).get_result()["status"] == "done"
    finally:
        DBOS.destroy()


def smoke(root: Path) -> None:
    pdf = html_to_pdf("<html><body><p>Offline PDF smoke check</p></body></html>")
    assert "Offline PDF smoke check" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    print("PASS: PDF rendered with readable text", flush=True)
    # The configured server is used only to create/drop these unique scratch DBs.
    base = make_url(db.database_url())
    name = f"biz_smoke_{uuid.uuid4().hex[:12]}"
    urls = [base.set(database=name), base.set(database=f"{name}_dbos")]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created: list[str] = []
    child: subprocess.Popen[str] | None = None
    try:
        with admin.connect() as connection:
            for url in urls:
                connection.execute(text(f'CREATE DATABASE "{url.database}"'))
                created.append(str(url.database))
        app_url = urls[0].render_as_string(hide_password=False)
        db.configure(app_url)
        migrations.upgrade(app_url)
        rt = runtime(root)
        with db.session_scope() as session:
            request = DemoRequest(
                sender="smoke@example.invalid", subject="Smoke check", body="Hello"
            )
            session.add(request)
            session.flush()
            packet_id = rt.bus.publish(
                session,
                type="service_request.received",
                from_part="smoke",
                summary="Smoke check",
                payload={
                    "request_id": str(request.id),
                    "channel": "desk",
                    "subject": request.subject,
                    "body": request.body,
                },
            ).id
        with tempfile.TemporaryDirectory(prefix="biz-smoke-") as tmp:
            sends = Path(tmp) / "sends.jsonl"
            env = {
                **os.environ,
                "BIZ_DATABASE_URL": app_url,
                "BIZ_DBOS_SYSTEM_URL": urls[1].render_as_string(hide_password=False),
                "BIZ_SMOKE_SENDS": str(sends),
                "BIZ_SMOKE_PACKET": str(packet_id),
            }
            command = [sys.executable, str(Path(__file__).resolve()), "--repo-root", str(root)]
            with (Path(tmp) / "worker.log").open("w+") as log:
                child = subprocess.Popen(
                    [*command, "--worker", "start"],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(f"worker exited before approval:\n{log.read()[-3000:]}")
                    with db.session_scope() as session:
                        run = session.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
                        if run is not None and run.status == "waiting":
                            task = session.get(Task, run.task_id)
                            op = session.get(StagedOperation, uuid.UUID(str(run.waiting_op_id)))
                            assert task is not None and task.status == "open"
                            assert op is not None and op.status == "pending"
                            assert (
                                op.tool == "demo_desk.send_reply"
                                and op.inputs["to"] == "smoke@example.invalid"
                                and op.inputs["subject"] == REPLY["subject"]
                                and op.inputs["body"] == REPLY["body"]
                            )
                            assert (
                                session.scalar(
                                    select(func.count())
                                    .select_from(Packet)
                                    .where(Packet.type == "message.sent")
                                )
                                == 0
                            )
                            break
                    time.sleep(0.1)
                else:
                    raise TimeoutError("worker did not reach approval within 60 seconds")
                assert not sends.exists(), "send happened before approval"
                child.kill()  # abrupt death while the durable workflow waits for approval
                child.wait(timeout=10)
                child = None
                print("PASS: pending approval, task open, zero sends; worker killed", flush=True)
                for mode in ("resume", "replay"):
                    result = subprocess.run(
                        [*command, "--worker", mode],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=90,
                    )
                    if result.returncode:
                        log.seek(0)
                        raise RuntimeError(f"{mode} failed:\n{log.read()[-3000:]}")
            deliveries = [json.loads(line) for line in sends.read_text().splitlines()]
            assert deliveries == [
                {
                    "key": op.inputs["idempotency_key"],
                    "to": [op.inputs["to"]],
                    "subject": op.inputs["subject"],
                    "body": op.inputs["body"],
                }
            ]
            with db.session_scope() as session:
                run = session.scalar(select(SopRun).where(SopRun.packet_id == packet_id))
                assert run is not None and run.status == "done"
                task = session.get(Task, run.task_id)
                assert task is not None and task.status == "done"
                kinds = set(
                    session.scalars(
                        select(EvidenceEvent.kind).where(EvidenceEvent.task_id == task.id)
                    )
                )
                assert {"approval.granted", "packet:message.sent"} <= kinds
                assert session.get(StagedOperation, op.id).status == "executed"
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Packet)
                        .where(Packet.type == "message.sent")
                    )
                    == 1
                )
            print("PASS: recovered, approved, completed with evidence; replay sent nothing again")
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=10)
        db.engine().dispose()
        with admin.connect() as connection:
            for database in reversed(created):
                connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--worker", choices=("start", "resume", "replay"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.repo_root.resolve(), args.worker)
    else:
        smoke(args.repo_root.resolve())
