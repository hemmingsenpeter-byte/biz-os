"""Acceptance inside the disposable Linux guest, before and after a real VM reboot.

Installed services stay shadow-only. Approval probes use the existing demo smoke
worker, fake providers and separate scratch databases with fixture authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pgserver
from biz_cap_documents.render import html_to_pdf
from biz_kernel import db
from biz_kernel.loader import load_composition
from biz_kernel.models.local_ollama import LocalModelError, literal_loopback_url
from biz_kernel.records.models import Task
from biz_kernel.tools.shadow import ExecutionMode, resolve_mode
from pypdf import PdfReader
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from deploy.upgrade_drill import assert_completed, deliveries, state

ROOT = Path(__file__).resolve().parents[1]
UNITS = (
    "biz-postgres.service",
    "biz-api.service",
    "biz-worker.service",
    "biz-web.service",
    "biz-comparison.service",
)
APPLICATION_UNITS = ("biz-api.service", "biz-worker.service", "biz-comparison.service")
QUIESCED_UNITS = tuple(unit for unit in UNITS if unit != "biz-postgres.service")
WORKER = r"""
import json, os, sys
from pathlib import Path
from deploy import smoke_demo as smoke
from biz_kernel import db, migrations
from dbos import DBOS
root = Path(sys.argv[1])
original = DBOS.__init__
def offline(self, *args, **kwargs):
    kwargs["config"] = {**kwargs["config"], "enable_otlp": False}
    original(self, *args, **kwargs)
DBOS.__init__ = offline
if sys.argv[2] == "seed":
    db.configure()
    migrations.upgrade(db.database_url())
    runtime = smoke.runtime(root)
    with db.session_scope() as session:
        request = smoke.DemoRequest(
            sender="vm-probe@example.invalid", subject="Virtual appliance probe",
            body="Synthetic fixture"
        )
        session.add(request)
        session.flush()
        packet = runtime.bus.publish(session, type="service_request.received", from_part="vm-probe",
            summary="Virtual appliance probe",
            payload={"request_id":str(request.id),"channel":"desk",
                "subject":request.subject,"body":request.body})
        result = {"packet_id":str(packet.id)}
    Path(os.environ["BIZ_VM_SEED"]).write_text(json.dumps(result))
else:
    smoke.worker(root, sys.argv[2])
"""


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".acceptance-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_environment(path: Path) -> None:
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.replace("_", "").isalnum():
            raise RuntimeError("invalid guest environment file")
        words = shlex.split(value, comments=False)
        if len(words) > 1:
            raise RuntimeError("guest environment values must be single quoted strings")
        os.environ[key] = words[0] if words else ""


def command(arguments: list[str], timeout: float = 60, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError(
            f"{Path(arguments[0]).name} failed ({result.returncode}): {result.stderr[-1500:]}"
        )
    return result.stdout.strip()


def source_sha256() -> str:
    digest = hashlib.sha256()
    excluded = {".venv", ".git", "node_modules", ".next", "__pycache__", ".pytest_cache"}
    paths: list[Path] = []
    for directory, children, filenames in os.walk(ROOT):
        children[:] = [name for name in children if name not in excluded]
        paths.extend(Path(directory) / name for name in filenames)
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def verify_reboot(checkpoint: dict[str, Any], current_boot: str, source: str) -> None:
    if uuid.UUID(str(checkpoint.get("boot_id"))) == uuid.UUID(current_boot):
        raise RuntimeError("the guest kernel has not rebooted since the checkpoint")
    if checkpoint.get("source_sha256") != source:
        raise RuntimeError("guest source changed across the reboot")
    if checkpoint.get("phase") not in {"awaiting_reboot", "verified_reboot"}:
        raise RuntimeError("no complete pre-reboot checkpoint is available")


def service_state() -> dict[str, Any]:
    services = {}
    for unit in UNITS:
        active = command(["systemctl", "is-active", unit])
        enabled = command(["systemctl", "is-enabled", unit])
        pid = int(command(["systemctl", "show", unit, "--property=MainPID", "--value"]))
        if active != "active" or enabled != "enabled" or pid <= 0:
            raise RuntimeError(f"{unit} is not enabled and running")
        services[unit] = {"active": active, "enabled": enabled, "pid": pid}
        if unit in APPLICATION_UNITS:
            environment = dict(
                entry.split("=", 1)
                for entry in Path(f"/proc/{pid}/environ").read_text().split("\0")
                if "=" in entry
            )
            for key in (
                "BIZ_EXECUTION_MODE",
                "BIZ_APP_DIR",
                "BIZ_DATABASE_URL",
                "BIZ_DBOS_SYSTEM_URL",
                "BIZ_REPO_ROOT",
                "BIZ_STORAGE_ROOT",
            ):
                if environment.get(key) != os.environ.get(key):
                    raise RuntimeError(f"{unit} does not use the acceptance environment")
    return services


def http_checks() -> dict[str, Any]:
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
        for origin in ("http://127.0.0.1:8000", "http://127.0.0.1:3000"):
            health = client.get(origin + "/v1/health")
            if health.status_code != 200 or health.json() != {"status": "ok"}:
                raise RuntimeError("API or owner-console proxy is unavailable")
            if client.get(origin + "/v1/auth/me").status_code != 401:
                raise RuntimeError("unauthenticated owner access was not refused")
        login = client.get("http://127.0.0.1:3000/auth/login")
        # Next renders a Suspense/loading shell before client hydration. The HTTP
        # probe verifies HTML and auth endpoints; browser acceptance checks the UI.
        if login.status_code != 200 or "<html" not in login.text.lower():
            raise RuntimeError("owner passkey login page is unavailable")
        refusal = client.post("http://127.0.0.1:3000/v1/auth/passkey/register/begin", json={})
        if refusal.status_code != 401:
            raise RuntimeError("passkey registration accepted an unauthenticated caller")
        options = client.post("http://127.0.0.1:3000/v1/auth/passkey/login/begin", json={})
        if (
            options.status_code != 200
            or options.json()["options"]["rpId"] != os.environ["BIZ_RP_ID"]
        ):
            raise RuntimeError("passkey challenge did not use the configured relying party")
    return {
        "health_and_proxy": True,
        "unauthenticated_access_refused": True,
        "registration_guard": True,
        "relying_party": os.environ["BIZ_RP_ID"],
        "passkey_ceremony": "not exercised by this command",
    }


def database_checks(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        version = int(connection.scalar(text("SHOW server_version_num")))
        extension = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if version // 10000 != 16 or not extension or revision != "0025":
        raise RuntimeError("the guest needs PostgreSQL 16, pgvector and schema 0025")
    return {"server_version_num": version, "pgvector": extension, "schema": revision}


def fake_shadow_configuration() -> dict[str, Any]:
    manifest = load_composition(Path(os.environ["BIZ_APP_DIR"]), ROOT)
    if not all(port.startswith("fake_") for port in manifest.ports.values()):
        raise RuntimeError("virtual acceptance requires entirely fake installed providers")
    mode = resolve_mode(
        manifest.execution_mode,
        manifest.supervised_authorization,
        ports=manifest.ports,
        policy=manifest.policy,
    )
    if mode != ExecutionMode.SHADOW:
        raise RuntimeError("installed guest services must remain shadow-only")
    for address in (
        "http://10.0.2.2:11434",
        "http://192.0.2.1:11434",
        "http://localhost:11434",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434/path",
    ):
        try:
            literal_loopback_url(address)
        except LocalModelError:
            continue
        raise RuntimeError("a nonliteral or nonlocal model URL was accepted")
    literal_loopback_url("http://127.0.0.1:11434")
    return {
        "installed_mode": "shadow",
        "business_providers": "fakes only",
        "strict_loopback_rejection_checks": True,
        "real_model_identity_and_inference": "not exercised; no model quality claim",
    }


def probe_environment(probe: dict[str, Any], directory: Path) -> dict[str, str]:
    url = make_url(os.environ["BIZ_DATABASE_URL"])
    return {
        **os.environ,
        "BIZ_DATABASE_URL": url.set(database=probe["database"]).render_as_string(
            hide_password=False
        ),
        "BIZ_DBOS_SYSTEM_URL": url.set(database=probe["workflow_database"]).render_as_string(
            hide_password=False
        ),
        "BIZ_EXECUTION_MODE": "supervised",
        "BIZ_SMOKE_PACKET": probe.get("packet_id", ""),
        "BIZ_SMOKE_SENDS": str(directory / "reboot-sends.jsonl"),
        "BIZ_VM_SEED": str(directory / "probe-seed.json"),
    }


def probe_process(probe: dict[str, Any], directory: Path, mode: str, timeout: float) -> None:
    with (directory / "probe.log").open("a") as log:
        result = subprocess.run(
            [sys.executable, "-c", WORKER, str(ROOT), mode],
            cwd=ROOT,
            env=probe_environment(probe, directory),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    if result.returncode:
        raise RuntimeError(f"synthetic approval {mode} failed; inspect the private guest probe.log")


def prepare_probe(directory: Path, timeout: float) -> dict[str, Any]:
    name = "biz_vm_" + uuid.uuid4().hex[:12]
    probe: dict[str, Any] = {"database": name, "workflow_database": name + "_dbos"}
    base = make_url(os.environ["BIZ_DATABASE_URL"])
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        for database in (name, name + "_dbos"):
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    admin.dispose()
    probe_process(probe, directory, "seed", timeout)
    probe.update(json.loads((directory / "probe-seed.json").read_text()))
    engine = create_engine(base.set(database=name))
    try:
        with (directory / "probe.log").open("a") as log:
            worker = subprocess.Popen(
                [sys.executable, "-c", WORKER, str(ROOT), "start"],
                cwd=ROOT,
                env=probe_environment(probe, directory),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    current = state(engine, probe["packet_id"])
                    if current and current["status"] == "waiting":
                        if (
                            current["task_status"] != "open"
                            or current["operation_status"] != "pending"
                        ):
                            raise RuntimeError(
                                "synthetic workflow did not retain its open approval"
                            )
                        break
                    if worker.poll() is not None:
                        raise RuntimeError("synthetic worker exited before reaching approval")
                    time.sleep(0.5)
                else:
                    raise TimeoutError("synthetic worker did not reach its durable approval")
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=30)
        if deliveries(directory / "reboot-sends.jsonl"):
            raise RuntimeError("synthetic provider was invoked before approval")
        probe["waiting"] = True
        return probe
    finally:
        engine.dispose()


def resume_probe(
    probe: dict[str, Any], directory: Path, timeout: float, *, after_reboot: bool = True
) -> dict[str, Any]:
    base = make_url(os.environ["BIZ_DATABASE_URL"])
    engine = create_engine(base.set(database=probe["database"]))
    try:
        current = state(engine, probe["packet_id"])
        if not current or current["status"] not in {"waiting", "done"}:
            raise RuntimeError("the pre-reboot approval is missing or changed")
        if current["status"] == "waiting":
            if deliveries(directory / "reboot-sends.jsonl"):
                raise RuntimeError("unexpected pre-approval provider invocation")
            probe_process(probe, directory, "resume", timeout)
        probe_process(probe, directory, "replay", timeout)
        assert_completed(engine, probe["packet_id"])
        if len(deliveries(directory / "reboot-sends.jsonl")) != 1:
            raise RuntimeError("recovery/replay did not produce exactly one fake send")
        return {
            "helper": "deploy.smoke_demo.worker",
            "real_dbos": True,
            "recovered_after_real_reboot": after_reboot,
            "fake_send_invocations": 1,
            "fixture_mode": "supervised in separate synthetic databases",
        }
    finally:
        engine.dispose()


def classic_smoke(directory: Path, timeout: float) -> dict[str, Any]:
    """Use the existing worker with TCG-appropriate deadlines, then check native PDF support."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    probe = prepare_probe(directory, timeout)
    try:
        result = resume_probe(probe, directory, timeout, after_reboot=False)
        label = "Synthetic virtual appliance PDF"
        pdf = html_to_pdf(f"<html><body><p>{label}</p></body></html>")
        if label not in PdfReader(io.BytesIO(pdf)).pages[0].extract_text():
            raise RuntimeError("guest PDF rendering did not preserve readable text")
        descriptor = os.open(
            directory / "rendered.pdf", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(pdf)
        return {**result, "pdf_readable_text": True, "pdf_sha256": hashlib.sha256(pdf).hexdigest()}
    finally:
        base = make_url(os.environ["BIZ_DATABASE_URL"])
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                for name in (probe["workflow_database"], probe["database"]):
                    connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            admin.dispose()


def storage_marker(directory: Path) -> dict[str, Any]:
    content = "Virtual appliance persistence " + uuid.uuid4().hex
    code = (
        "import json,sys; from biz_port_storage_local import create; "
        "s=create(); o=s.put(sys.argv[1].encode(),'vm-marker.txt','text/plain'); "
        "assert s.get(o.uri)==sys.argv[1].encode(); "
        "print(json.dumps({'uri':o.uri,'sha256':o.sha256}))"
    )
    marker = json.loads(
        command(["runuser", "-u", "biz", "--", sys.executable, "-c", code, content])
    )
    with db.session_scope() as session:
        task = Task(title=content, kind="virtual_acceptance", status="open")
        session.add(task)
        session.flush()
        marker.update(task_id=str(task.id), title=content)
    write_json(directory / "marker.json", marker)
    return marker


def verify_marker(marker: dict[str, Any], engine: Engine) -> None:
    code = (
        "import sys,hashlib; from biz_port_storage_local import create; "
        "assert hashlib.sha256(create().get(sys.argv[1])).hexdigest()==sys.argv[2]"
    )
    command(
        ["runuser", "-u", "biz", "--", sys.executable, "-c", code, marker["uri"], marker["sha256"]]
    )
    with engine.connect() as connection:
        title = connection.scalar(
            text("SELECT title FROM tasks WHERE id=CAST(:id AS uuid)"), {"id": marker["task_id"]}
        )
        if title != marker["title"]:
            raise RuntimeError("the persistent application marker is missing or changed")


def backup_marker(directory: Path, marker: dict[str, Any], timeout: float) -> dict[str, Any]:
    binary = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin"
    url = make_url(os.environ["BIZ_DATABASE_URL"])
    environment = {
        **os.environ,
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "biz",
        "PGPASSWORD": url.password or "",
    }
    dumps = []
    command(["systemctl", "stop", *QUIESCED_UNITS], timeout)
    try:
        for database in (url.database, make_url(os.environ["BIZ_DBOS_SYSTEM_URL"]).database):
            path = directory / f"{database}.dump"
            command(
                [str(binary / "pg_dump"), "--format=custom", "--file", str(path)],
                timeout,
                {**environment, "PGDATABASE": str(database)},
            )
            command([str(binary / "pg_restore"), "--list", str(path)], timeout)
            dumps.append(
                {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
    finally:
        # Requires= dependencies are stopped too; starting their parent alone
        # does not reactivate the console or comparison consumer.
        command(["systemctl", "start", *QUIESCED_UNITS], timeout)
    restore = "biz_vm_restore_" + uuid.uuid4().hex[:12]
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{restore}"'))
        command(
            [
                str(binary / "pg_restore"),
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--no-comments",
                "--dbname",
                restore,
                dumps[0]["path"],
            ],
            timeout,
            environment,
        )
        restored = create_engine(url.set(database=restore))
        try:
            with restored.connect() as connection:
                title = connection.scalar(
                    text("SELECT title FROM tasks WHERE id=CAST(:id AS uuid)"),
                    {"id": marker["task_id"]},
                )
                if title != marker["title"]:
                    raise RuntimeError("native database backup did not restore the marker")
        finally:
            restored.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{restore}" WITH (FORCE)'))
        admin.dispose()
    return {
        "database_dumps": dumps,
        "marker_restored_to_scratch_database": True,
        "scope": "same-guest quiesced logical backup; not encrypted off-box disaster recovery",
    }


def execute(directory: Path, verify: bool, timeout: float) -> dict[str, Any]:
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise RuntimeError("run acceptance as root inside the disposable Linux guest")
    if sys.flags.optimize or os.environ.get("PYTHONOPTIMIZE") not in {None, "", "0"}:
        raise RuntimeError("the existing smoke requires Python assertions to be enabled")
    source, current_boot = source_sha256(), boot_id()
    checkpoint_path = directory / "checkpoint.json"
    if not verify and checkpoint_path.exists():
        raise RuntimeError("a checkpoint already exists; reboot the guest and use --verify-reboot")
    checkpoint = json.loads(checkpoint_path.read_text()) if verify else None
    if checkpoint is not None:
        verify_reboot(checkpoint, current_boot, source)
    configuration = fake_shadow_configuration()
    deadline = time.monotonic() + timeout
    while True:
        try:
            services = service_state()
            web = http_checks()
            break
        except (RuntimeError, httpx.HTTPError, FileNotFoundError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    db.configure(os.environ["BIZ_DATABASE_URL"])
    engine = db.engine()
    try:
        postgres = database_checks(engine)
        if checkpoint is None:
            smoke = classic_smoke(directory / "smoke", timeout)
            marker = storage_marker(directory)
            backup = backup_marker(directory, marker, timeout)
            probe = prepare_probe(directory, timeout)
            services = service_state()
            if source_sha256() != source:
                raise RuntimeError("guest source changed during acceptance")
            checkpoint = {
                "phase": "awaiting_reboot",
                "boot_id": current_boot,
                "source_sha256": source,
                "marker": marker,
                "backup": backup,
                "probe": probe,
                "smoke": smoke,
            }
            write_json(checkpoint_path, checkpoint)
            recovery: dict[str, Any] = {
                "checkpointed_pending_approval": True,
                "fake_send_invocations": 0,
            }
            status = "AWAITING_REBOOT"
        else:
            verify_marker(checkpoint["marker"], engine)
            for dump in checkpoint["backup"]["database_dumps"]:
                if hashlib.sha256(Path(dump["path"]).read_bytes()).hexdigest() != dump["sha256"]:
                    raise RuntimeError("a pre-reboot native backup changed")
            recovery = resume_probe(checkpoint["probe"], directory, timeout)
            if source_sha256() != source:
                raise RuntimeError("guest source changed during reboot verification")
            checkpoint["phase"] = "verified_reboot"
            write_json(checkpoint_path, checkpoint)
            status = "PASS"
        services = service_state()
        web = http_checks()
        if source_sha256() != source:
            raise RuntimeError("guest source changed during acceptance")
        memory_kib = int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemTotal:")
            )
        )
        disk = shutil.disk_usage(directory)
        return {
            "status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
            "source_commit": (ROOT / "SOURCE_COMMIT").read_text().strip(),
            "source_sha256": source,
            "guest": {
                "system": platform.system(),
                "architecture": platform.machine(),
                "kernel": platform.release(),
                "cpu_count": os.cpu_count(),
                "memory_bytes": memory_kib * 1024,
                "disk_total_bytes": disk.total,
                "disk_free_bytes": disk.free,
                "boot_id": current_boot,
                "virtualization": command(["systemd-detect-virt"]),
            },
            "services": services,
            "postgres": postgres,
            "http_auth_boundaries": web,
            "configuration": configuration,
            "pdf_approval_crash_replay_smoke": checkpoint["smoke"],
            "reboot_verified": verify,
            "persistent_approval": recovery,
            "persistent_marker": checkpoint["marker"],
            "native_backup": checkpoint["backup"],
            "checkpoint": str(checkpoint_path),
            "limitations": [
                "Linux CPU-emulation integration surrogate; "
                "not macOS packaging or Metal performance",
                "No live providers, real-model quality or held-out/customer evidence",
                "No FileVault, Keychain, Mac power recovery or physical passkey acceptance",
            ],
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/biz-os/virtual.env"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/biz-os/acceptance"))
    parser.add_argument("--out", type=Path, default=Path("/var/lib/biz-os/acceptance/latest.json"))
    parser.add_argument("--verify-reboot", action="store_true")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    write_json(args.out, {"status": "RUNNING", "reboot_mode": args.verify_reboot})
    try:
        load_environment(args.env_file)
        args.state_dir.mkdir(parents=True, exist_ok=True)
        args.state_dir.chmod(0o700)
        result = execute(args.state_dir, args.verify_reboot, args.timeout)
    except (Exception, KeyboardInterrupt) as exc:
        write_json(
            args.out,
            {
                "status": "UNVERIFIED",
                "error_type": type(exc).__name__,
                "reboot_mode": args.verify_reboot,
            },
        )
        raise
    write_json(args.out, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
