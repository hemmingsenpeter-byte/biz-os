"""Pinned CPU-only Ollama bootstrap and synthetic probe for the Linux virtual appliance.

The small model proves execution only. It does not establish a business quality gate
or represent Apple Silicon/macOS performance. No business provider or customer input
is used by this helper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path, PurePosixPath
from typing import IO, Any

OLLAMA_VERSION = "0.40.19"
RUNTIME_URL = (
    f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/"
    "ollama-linux-amd64.tar.zst"
)
RUNTIME_BYTES = 1_649_689_331
RUNTIME_SHA256 = "8eb99f9dd91c82f4c51a2c891415bfbdf07bd43f627d82514c47dfd613d2070f"
MAX_CPU_BYTES = 300 * 1024 * 1024
MODEL = "smollm2:135m-instruct-q4_K_M"
MODEL_MANIFEST_SHA256 = "fd316850965252d917ac2bf811adef10ba91e129e5f2809f2a24a8501ff73e4f"
REGISTRY = "https://registry.ollama.ai/v2/library/smollm2"
ENDPOINT = "http://127.0.0.1:11434"


def _request(url: str) -> Any:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "biz-os-virtual-appliance"}), timeout=60
    )


def _cpu_path(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("archive contains an unsafe path")
    if path == PurePosixPath("bin/ollama") or (
        len(path.parts) == 3 and path.parts[:2] == ("lib", "ollama")
    ):
        return path
    return None


def extract_cpu(
    stream: IO[bytes], destination: Path, *, limit: int = MAX_CPU_BYTES
) -> dict[str, Any]:
    """Read a tar stream without extracting GPU subdirectories or special files."""
    retained = 0
    links: list[tuple[Path, str, bool]] = []
    files: dict[str, str] = {}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            relative = _cpu_path(member.name)
            if relative is None or member.isdir():
                continue
            target = destination / relative
            if target.exists() or target.is_symlink():
                raise ValueError("archive contains a repeated CPU path")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isfile():
                retained += member.size
                if retained > limit:
                    raise ValueError("CPU-only runtime exceeds its retained disk budget")
                source_stream = archive.extractfile(member)
                if source_stream is None:
                    raise ValueError("CPU runtime archive member is unreadable")
                checksum = hashlib.sha256()
                with target.open("xb") as output:
                    while block := source_stream.read(1024 * 1024):
                        output.write(block)
                        checksum.update(block)
                target.chmod(member.mode & 0o777)
                files[str(relative)] = checksum.hexdigest()
            elif member.issym():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute() or len(link_path.parts) != 1 or "\\" in member.linkname:
                    raise ValueError("CPU runtime symlink must stay in its own directory")
                links.append((target, member.linkname, True))
            elif member.islnk():
                hardlink_path = _cpu_path(member.linkname)
                if hardlink_path is None:
                    raise ValueError("CPU runtime hardlink leaves the selected runtime")
                links.append((target, str(hardlink_path), False))
            else:
                raise ValueError("CPU runtime contains an unsupported special file")
    for target, link_text, symbolic in links:
        if symbolic:
            target.symlink_to(link_text)
        else:
            source_path = destination / link_text
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError("CPU runtime hardlink target is unavailable")
            os.link(source_path, target)
    for target, _, _ in links:
        if not target.resolve().is_relative_to(destination.resolve()) or not target.is_file():
            raise ValueError("CPU runtime link is dangling or outside the selected runtime")
    if "bin/ollama" not in files:
        raise ValueError("CPU runtime is missing the Ollama executable")
    return {
        "retained_bytes": retained,
        "files": files,
        "links": {str(path.relative_to(destination)): link for path, link, _ in links},
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".virtual-model-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage_runtime(destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("runtime destination already exists; preserve its verification evidence")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".ollama-unverified-"))
    decoder = subprocess.Popen(
        ["zstd", "-d", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    observed: dict[str, Any] = {"bytes": 0}

    def feed() -> None:
        checksum = hashlib.sha256()
        try:
            assert decoder.stdin is not None
            with _request(RUNTIME_URL) as response:
                while block := response.read(1024 * 1024):
                    observed["bytes"] += len(block)
                    if observed["bytes"] > RUNTIME_BYTES:
                        raise ValueError("runtime download exceeds the pinned size")
                    checksum.update(block)
                    decoder.stdin.write(block)
            observed["sha256"] = checksum.hexdigest()
        except BaseException as exc:
            observed["error"] = type(exc).__name__
        finally:
            if decoder.stdin is not None:
                decoder.stdin.close()

    transfer = threading.Thread(target=feed, daemon=True)
    transfer.start()
    try:
        assert decoder.stdout is not None
        inventory = extract_cpu(decoder.stdout, staging)
        while decoder.stdout.read(1024 * 1024):
            pass  # consume archive trailers so the hash covers every compressed byte
        transfer.join(timeout=90)
        stderr = decoder.stderr.read() if decoder.stderr else b""
        code = decoder.wait(timeout=30)
        if transfer.is_alive() or observed.get("error") or code:
            raise ValueError("runtime download or decompression did not complete")
        if observed["bytes"] != RUNTIME_BYTES or observed.get("sha256") != RUNTIME_SHA256:
            raise ValueError("official runtime archive failed its pinned whole-file verification")
        if stderr and b"error" in stderr.lower():
            raise ValueError("runtime decompressor reported an error")
        report = {
            "version": OLLAMA_VERSION,
            "archive_url": RUNTIME_URL,
            "archive_bytes": observed["bytes"],
            "archive_sha256": RUNTIME_SHA256,
            "cpu_only": True,
            **inventory,
        }
        _write_json(staging / "cpu-runtime.json", report)
        staging.replace(destination)
        return report
    finally:
        if decoder.poll() is None:
            decoder.kill()
        if decoder.stdout is not None:
            decoder.stdout.close()
        if decoder.stderr is not None:
            decoder.stderr.close()
        if staging.exists():
            shutil.rmtree(staging)


def _download_blob(url: str, target: Path, checksum: str, size: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with _request(url) as response, target.open("xb") as output:
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > size:
                raise ValueError("model blob exceeds its pinned size")
            digest.update(block)
            output.write(block)
    if total != size or digest.hexdigest() != checksum:
        raise ValueError("model blob failed exact SHA-256 verification")


def stage_model(destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("model destination already exists; preserve its verification evidence")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".model-unverified-"))
    try:
        with _request(REGISTRY + "/manifests/135m-instruct-q4_K_M") as response:
            manifest = response.read(64 * 1024)
        if hashlib.sha256(manifest).hexdigest() != MODEL_MANIFEST_SHA256:
            raise ValueError("model tag changed from its approved manifest")
        data = json.loads(manifest)
        blobs = [data["config"], *data["layers"]]
        if sum(row["size"] for row in blobs) > 110 * 1024 * 1024:
            raise ValueError("model exceeds its approved retained budget")
        for row in blobs:
            algorithm, checksum = row["digest"].split(":", 1)
            if (
                algorithm != "sha256"
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
            ):
                raise ValueError("model manifest contains an invalid blob identity")
            _download_blob(
                REGISTRY + "/blobs/" + row["digest"],
                staging / "blobs" / f"sha256-{checksum}",
                checksum,
                row["size"],
            )
        path = staging / "manifests/registry.ollama.ai/library/smollm2/135m-instruct-q4_K_M"
        path.parent.mkdir(parents=True)
        path.write_bytes(manifest)
        report = {
            "model": MODEL,
            "manifest_sha256": MODEL_MANIFEST_SHA256,
            "total_blob_bytes": sum(row["size"] for row in blobs),
            "blobs": blobs,
            "synthetic_functional_use_only": True,
        }
        _write_json(staging / "model-verification.json", report)
        staging.replace(destination)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_model_store(models: Path) -> None:
    manifest_path = models / "manifests/registry.ollama.ai/library/smollm2/135m-instruct-q4_K_M"
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MODEL_MANIFEST_SHA256:
        raise ValueError("staged model manifest changed")
    manifest = json.loads(raw)
    for blob in [manifest["config"], *manifest["layers"]]:
        path = models / "blobs" / blob["digest"].replace(":", "-", 1)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != blob["size"] or "sha256:" + actual != blob["digest"]:
            raise ValueError("staged model blob changed")


def install(runtime: Path, models: Path) -> None:
    if os.geteuid() != 0:
        raise ValueError("guest service installation needs root")
    report = json.loads((runtime / "cpu-runtime.json").read_text())
    if report.get("archive_sha256") != RUNTIME_SHA256:
        raise ValueError("runtime bootstrap verification is missing")
    for relative, expected in report["files"].items():
        if hashlib.sha256((runtime / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("staged CPU runtime changed")
    verify_model_store(models)
    runtime.chmod(0o755)
    for path in runtime.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    result = subprocess.run(["id", "biz-ollama"], capture_output=True)
    if result.returncode:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--home-dir",
                "/var/lib/biz-os/ollama",
                "--shell",
                "/usr/sbin/nologin",
                "biz-ollama",
            ],
            check=True,
        )
    state = Path("/var/lib/biz-os/ollama")
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    target = state / "models"
    shutil.copytree(models, target, dirs_exist_ok=True)
    verify_model_store(target)
    subprocess.run(["chown", "-R", "biz-ollama:biz-ollama", str(state)], check=True)
    unit = """[Unit]
Description=BIZ OS virtual appliance synthetic CPU model
After=network.target
[Service]
Type=simple
User=biz-ollama
Group=biz-ollama
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_MODELS=/var/lib/biz-os/ollama/models
Environment=OLLAMA_NO_CLOUD=1
Environment=OLLAMA_CONTEXT_LENGTH=256
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_KEEP_ALIVE=30m
Environment=CUDA_VISIBLE_DEVICES=-1
Environment=LD_LIBRARY_PATH=RUNTIME/lib/ollama
ExecStart=RUNTIME/bin/ollama serve
Restart=on-failure
RestartSec=2
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/biz-os/ollama
NoNewPrivileges=true
MemoryMax=2G
[Install]
WantedBy=multi-user.target
""".replace("RUNTIME", str(runtime.resolve()))
    Path("/etc/systemd/system/biz-ollama.service").write_text(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "biz-ollama.service"], check=True)


def probe(output: Path, *, timeout: float = 600) -> dict[str, Any]:
    import asyncio
    import platform
    from dataclasses import asdict

    from biz_kernel.models.local_ollama import verify_local_ollama
    from biz_kernel.models.router import Router
    from biz_port_model_ollama import OllamaModel

    class SyntheticModel(OllamaModel):
        async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            payload = {
                **body,
                "options": {
                    "temperature": 0,
                    "seed": 7,
                    "num_ctx": 256,
                    "num_predict": 32,
                    "num_thread": 2,
                },
            }
            return await super()._post(path, payload)

    before = verify_local_ollama(
        base_url=ENDPOINT, model=MODEL, digest=MODEL_MANIFEST_SHA256, ollama_version=OLLAMA_VERSION
    )
    started = time.monotonic()
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ready"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    result = asyncio.run(
        Router(local=SyntheticModel(ENDPOINT, MODEL, timeout=timeout), cloud=None).generate(
            "synthetic.status",
            "Return a JSON object with status ready.",
            [],
            schema,
            tier_hint="local",
            untrusted=["Fictional local status fixture. No customer information."],
            escalate=False,
        )
    )
    after = verify_local_ollama(
        base_url=ENDPOINT, model=MODEL, digest=MODEL_MANIFEST_SHA256, ollama_version=OLLAMA_VERSION
    )
    if result.usage.input_tokens <= 0 or result.usage.output_tokens <= 0:
        raise ValueError("the real model did not report input and generated token counts")
    report = {
        "kind": "synthetic_actual_local_model_inference",
        "execution": {
            "system": platform.system(),
            "machine": platform.machine(),
            "kernel": platform.release(),
            "cpus": os.cpu_count(),
            "virtualization": subprocess.run(
                ["systemd-detect-virt", "--vm"], capture_output=True, text=True
            ).stdout.strip()
            if shutil.which("systemd-detect-virt")
            else "unknown",
        },
        "model_before": before,
        "model_after": after,
        "source_context": {
            "task": "synthetic.status",
            "backend": f"{SyntheticModel.__module__}.{SyntheticModel.__qualname__}",
            "generation_settings": {
                "temperature": 0,
                "seed": 7,
                "num_ctx": 256,
                "num_predict": 32,
                "num_thread": 2,
            },
            "system": "Return a JSON object with status ready.",
            "schema": schema,
            "untrusted_text": "Fictional local status fixture. No customer information.",
        },
        "output": result.output,
        "usage": {**asdict(result.usage), "cost_usd": str(result.cost_usd)},
        "attempts": result.attempts,
        "elapsed_seconds": time.monotonic() - started,
        "business_providers_used": False,
        "customer_data_used": False,
        "business_quality_gate_established": False,
        "mac_mini_performance_representative": False,
    }
    _write_json(output, report)
    return report


def paired(output: Path, env_file: Path) -> dict[str, Any]:
    """Use fictional data and genuine observed inference in isolated disposable databases."""
    import uuid

    from biz_kernel import db, migrations
    from biz_kernel.approvals import staged
    from biz_kernel.approvals.models import Correction
    from biz_kernel.models_registry import import_all_models
    from biz_kernel.policy.types import Action, SideEffect
    from biz_kernel.training import cohorts, comparison, service
    from biz_kernel.workflows.models import SopRun
    from biz_kernel.workflows.worker import dbos_system_url, ensure_database
    from dbos import DBOS
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url

    settings = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    original = make_url(settings["BIZ_DATABASE_URL"])
    name = "virtual_model_" + uuid.uuid4().hex[:12]
    application_url = original.set(database=name).render_as_string(hide_password=False)
    system_url = dbos_system_url(application_url)
    system_name = make_url(system_url).database
    admin = create_engine(original.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created: list[str] = []
    engine = None
    report: dict[str, Any] = {
        "kind": "synthetic_actual_local_paired_comparison",
        "customer_data_used": False,
        "business_providers_used": False,
        "business_quality_gate_established": False,
        "mac_mini_performance_representative": False,
        "isolated_databases": [name, system_name],
        "databases_removed": False,
    }
    _write_json(output, report)
    try:
        draft = probe(output.with_name(output.stem + "-draft.json"))
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        ensure_database(system_url)
        assert system_name is not None
        created.append(system_name)
        engine = db.configure(application_url)
        import_all_models()
        migrations.upgrade(application_url)
        context = draft["source_context"]
        identity = draft["model_after"]
        with db.session_scope() as session:
            op = staged.stage(
                session,
                Action(
                    actor="synthetic_probe",
                    tool="virtual.record",
                    side_effect=SideEffect.DRAFT,
                    inputs={"note": "An observed synthetic model output"},
                    decision_type="synthetic_test",
                ),
                "virtual-appliance-fixture",
            )
            run = SopRun(
                id="virtual-probe-" + uuid.uuid4().hex,
                role="synthetic_probe",
                sop="capture",
                waiting_op_id=str(op.id),
            )
            session.add(run)
            session.flush()
            snapshot = service.capture_draft(
                session,
                run_id=run.id,
                step_id="draft",
                actor="synthetic_probe",
                task=context["task"],
                prompt_path="deploy/virtual_model.py",
                system=context["system"],
                schema=context["schema"],
                untrusted_text=context["untrusted_text"],
                output=draft["output"],
                tier="local",
                backend=context["backend"],
                configured_model=MODEL,
                model_revision=MODEL_MANIFEST_SHA256,
            )
            staged.edit(
                session,
                op.id,
                {"note": "Owner-labelled fictional regression fixture"},
                by="virtual-appliance-fixture",
                reason="Exercise versioned testing with fictional data.",
                severity="cosmetic",
                version=op.version,
            )
            correction = session.scalar(select(Correction).where(Correction.op_id == op.id))
            assert correction is not None
            candidate = service.create_candidate(
                session,
                correction.id,
                rule="Keep the synthetic status in JSON.",
                by="virtual-appliance-fixture",
                snapshot_id=snapshot.id,
                expected_output={"status": "ready"},
            )
            version = service.create_version(
                session,
                candidate.id,
                parent_id=None,
                rule="Keep the synthetic status in JSON.",
                system="Return a JSON object with status ready. Respond only with JSON.",
                expected_output={"status": "ready"},
                by="virtual-appliance-fixture",
            )
            service.decide_version(
                session, version.id, decision="accepted_for_testing", by="virtual-appliance-fixture"
            )
            cohort = cohorts.create_cohort(
                session,
                name="Fictional virtual appliance fixture",
                version_ids=[version.id],
                by="virtual-appliance-fixture",
            )
            cohorts.attest_cohort(
                session,
                cohort.id,
                by="virtual-appliance-fixture",
                reason="Fictional tuning data for execution testing only.",
            )
            job = cohorts.enqueue(
                session,
                version_id=version.id,
                cohort_id=cohort.id,
                model={
                    "name": MODEL,
                    "digest": MODEL_MANIFEST_SHA256,
                    "ollama_version": OLLAMA_VERSION,
                    "backend_version": identity["backend_version"],
                    "base_url": ENDPOINT,
                    "temperature": 0,
                    "seed": 7,
                    "num_predict": 32,
                    "timeout_seconds": 600,
                    "repetitions": 1,
                },
                idempotency_key="virtual-actual-model-pair",
                by="virtual-appliance-fixture",
            )
            job_id = job.id
            report.update(
                source_snapshot_sha256=snapshot.payload_sha256,
                version_sha256=version.payload_sha256,
                cohort_sha256=cohort.payload_sha256,
                comparison_request_sha256=job.payload_sha256,
                model=identity,
            )
        DBOS(
            config={
                "name": "biz-virtual-model",
                "system_database_url": system_url,
                "application_version": comparison.WORKFLOW_VERSION,
            }
        )  # type: ignore[arg-type]
        DBOS.launch()
        started = time.monotonic()
        report["comparison"] = comparison.start_comparison(job_id).get_result()
        report["elapsed_seconds"] = time.monotonic() - started
        with db.session_scope() as session:
            rows = cohorts.events(session, job_id)
            report["event_counts"] = {
                kind: sum(row.kind == kind for row in rows) for kind in {row.kind for row in rows}
            }
        # A repeated request must return the same sealed result without further inference.
        repeated = comparison.start_comparison(job_id).get_result()
        report["replay_matches"] = repeated == report["comparison"]
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=10)
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            for database in reversed(created):
                connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        report["databases_removed"] = True
        _write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage-runtime", "stage-model"):
        command = commands.add_parser(name)
        command.add_argument("destination", type=Path)
    command = commands.add_parser("install")
    command.add_argument("--runtime", type=Path, required=True)
    command.add_argument("--models", type=Path, required=True)
    command = commands.add_parser("probe")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--timeout", type=float, default=600)
    command = commands.add_parser("paired")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--env-file", type=Path, default=Path("/etc/biz-os/virtual.env"))
    args = parser.parse_args()
    if args.command == "stage-runtime":
        result = stage_runtime(args.destination)
    elif args.command == "stage-model":
        result = stage_model(args.destination)
    elif args.command == "install":
        install(args.runtime, args.models)
        result = {"service": "biz-ollama", "endpoint": ENDPOINT}
    elif args.command == "probe":
        result = probe(args.output, timeout=args.timeout)
    else:
        result = paired(args.output, args.env_file)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
