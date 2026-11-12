"""Disposable real API/DBOS/Next stack for the owner-console browser acceptance test.

Run through `pnpm --dir web test:e2e` after building the web app. This uses its own
pgserver instance and fake ports; it never uses BIZ_DATABASE_URL from the caller.
The owner invite is produced by the real CLI. Browser tests enrol and sign in with
a Chromium virtual passkey through the normal HTTP endpoints.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pgserver
from biz_cap_demo_desk.models import DemoRequest
from biz_kernel import db, migrations
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.loader import load_composition
from biz_kernel.runtime import build_runtime
from biz_kernel.workflows.models import SopRun
from biz_kernel.workflows.worker import ensure_database
from biz_port_fakes import FakeMail, FakeModel
from dbos import DBOS
from sqlalchemy import make_url, select

ROOT = Path(__file__).resolve().parents[1]
REPLY = {
    "subject": "Owner console acceptance",
    "body": "Exact approved text: <img src=x onerror=alert(1)> & thank you.",
    "confidence": 0.95,
}


class ObservedMail(FakeMail):
    async def send(self, msg: Any, idempotency_key: str) -> Any:
        with Path(os.environ["BIZ_BROWSER_SENDS"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {"key": idempotency_key, "to": msg.to, "subject": msg.subject, "body": msg.body}
                )
                + "\n"
            )
        return await super().send(msg, idempotency_key)


def seed() -> dict[str, Any]:
    manifest = load_composition(ROOT / "apps/demo", ROOT)
    if any(not name.startswith("fake_") for name in manifest.ports.values()):
        raise RuntimeError("browser acceptance requires entirely fake demo ports")
    rt = build_runtime(
        ROOT / "apps/demo",
        ROOT,
        ports={"mail": ObservedMail(), "model_local": FakeModel([REPLY] * 3)},
        approval_poll_seconds=1,
    )
    DBOS(
        config={
            "name": "biz-owner-browser",
            "system_database_url": os.environ["BIZ_DBOS_SYSTEM_URL"],
            "application_version": "owner-browser-v1",
        }
    )
    DBOS.launch()
    packets = {}
    for case in ("approve", "reject", "stale"):
        with db.session_scope() as session:
            request = DemoRequest(
                sender=f"{case}@example.invalid", subject=f"Browser {case}", body="Please reply."
            )
            session.add(request)
            session.flush()
            packets[case] = rt.bus.publish(
                session,
                type="service_request.received",
                from_part="browser_acceptance",
                summary=request.subject,
                payload={
                    "request_id": str(request.id),
                    "channel": "desk",
                    "subject": request.subject,
                    "body": request.body,
                },
            ).id
    rt.tick()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        operations: dict[str, Any] = {}
        with db.session_scope() as session:
            for case, packet in packets.items():
                run = session.scalar(select(SopRun).where(SopRun.packet_id == packet))
                if run is None or run.status != "waiting" or run.waiting_op_id is None:
                    break
                op = session.get(StagedOperation, uuid.UUID(run.waiting_op_id))
                if op is None or op.status != "pending":
                    break
                operations[case] = {
                    "id": str(op.id),
                    "task_id": str(run.task_id),
                    "version": op.version,
                    "inputs": op.inputs,
                }
        if len(operations) == 3:
            return operations
        time.sleep(0.1)
    raise TimeoutError("browser demo did not stage its three approval requests")


def stop_process(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def serve_runtime() -> None:
    """Keep DBOS in its own process so teardown always has a bounded kill fallback."""
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    db.configure()
    try:
        operations = seed()
        operations_path = Path(os.environ["BIZ_BROWSER_OPERATIONS"])
        temporary = operations_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(operations))
        temporary.replace(operations_path)
        stopped.wait()
    finally:
        with db.session_scope() as session:
            pending = list(session.scalars(select(SopRun.id).where(SopRun.status == "waiting")))
        for workflow in pending:
            DBOS.cancel_workflow(workflow)
        DBOS.destroy(workflow_completion_timeout_sec=1)
        db.engine().dispose()


def wait_http(
    url: str,
    processes: list[subprocess.Popen[Any]],
    stopped: threading.Event,
    timeout: int = 60,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stopped.is_set():
            raise SystemExit(0)
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("a browser acceptance server exited during startup")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"browser acceptance server not ready: {url}")


def main() -> None:
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    port = int(os.environ.get("BIZ_BROWSER_PORT", "3100"))
    api_port = int(os.environ.get("BIZ_BROWSER_API_PORT", "8100"))
    origin = f"http://localhost:{port}"
    state_path = Path(
        os.environ.get("BIZ_BROWSER_STATE", ROOT / "web/test-results/browser-state.json")
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    processes: list[subprocess.Popen[Any]] = []
    with tempfile.TemporaryDirectory(prefix="biz-browser-") as temporary:
        directory = Path(temporary)
        server = pgserver.get_server(directory / "postgres", cleanup_mode="delete")
        try:
            base = make_url(
                server.get_uri("biz_browser").replace("postgresql://", "postgresql+psycopg://", 1)
            )
            app_url = base.render_as_string(hide_password=False)
            system_url = base.set(database="biz_browser_dbos").render_as_string(hide_password=False)
            os.environ.update(
                BIZ_DATABASE_URL=app_url,
                BIZ_DBOS_SYSTEM_URL=system_url,
                BIZ_SECRET_KEY=secrets.token_urlsafe(48),
                BIZ_ORIGIN=origin,
                BIZ_RP_ID="localhost",
                BIZ_APP_DIR=str(ROOT / "apps/demo"),
                BIZ_REPO_ROOT=str(ROOT),
                BIZ_BROWSER_SENDS=str(directory / "sends.jsonl"),
                BIZ_BROWSER_OPERATIONS=str(directory / "operations.json"),
                BIZ_API_URL=f"http://127.0.0.1:{api_port}",
            )
            ensure_database(app_url)
            ensure_database(system_url)
            db.configure(app_url)
            migrations.upgrade(app_url)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "biz_kernel.cli.main",
                    "owner-invite",
                    "apps/demo",
                    "--email",
                    "owner@example.invalid",
                    "--name",
                    "Browser Owner",
                    "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            owner = json.loads(result.stdout)
            runtime_process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                cwd=ROOT,
                start_new_session=True,
            )
            processes.append(runtime_process)
            operations_path = Path(os.environ["BIZ_BROWSER_OPERATIONS"])
            deadline = time.monotonic() + 60
            while not operations_path.exists():
                if stopped.is_set():
                    raise SystemExit(0)
                if runtime_process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("browser demo worker did not stage its approval requests")
                time.sleep(0.1)
            operations = json.loads(operations_path.read_text())
            state_path.write_text(
                json.dumps(
                    {
                        "owner": owner,
                        "operations": operations,
                        "sends": os.environ["BIZ_BROWSER_SENDS"],
                    }
                )
            )
            state_path.chmod(0o600)
            standalone = ROOT / "web/.next/standalone"
            if not (standalone / "server.js").is_file():
                raise RuntimeError("build the web app before running browser acceptance")
            shutil.copytree(
                ROOT / "web/.next/static", standalone / ".next/static", dirs_exist_ok=True
            )
            if (ROOT / "web/public").is_dir():
                shutil.copytree(ROOT / "web/public", standalone / "public", dirs_exist_ok=True)
            os.environ.update(PORT=str(port), HOSTNAME="127.0.0.1")
            commands = [
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "biz_kernel.api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                ["node", str(standalone / "server.js")],
            ]
            for command in commands:
                processes.append(subprocess.Popen(command, cwd=ROOT, start_new_session=True))
            wait_http(f"http://127.0.0.1:{api_port}/v1/health", processes, stopped)
            wait_http(f"{origin}/auth/login", processes, stopped)
            wait_http(f"{origin}/v1/health", processes, stopped)
            print("Owner console browser acceptance stack ready", flush=True)
            while not stopped.wait(0.5):
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("a browser acceptance server stopped unexpectedly")
        finally:
            for process in reversed(processes):
                stop_process(process)
            db.engine().dispose()
            state_path.unlink(missing_ok=True)
            server.cleanup()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        serve_runtime()
    else:
        main()
