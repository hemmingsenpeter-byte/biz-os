"""Disposable operator probe for the BIZ OS plan's receptionist smoke cases."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pgserver
import yaml
from biz_cap_comms.models import Outbox
from biz_cap_intake.models import Enquiry
from biz_kernel import db, migrations
from biz_kernel.api.app import create_app
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.audit.log import AuditEntry
from biz_kernel.contracts.ports import InboundMessage
from biz_kernel.evidence.models import EvidenceEvent
from biz_kernel.packets.bus import Packet
from biz_kernel.runtime import build_runtime, capability_routes
from biz_kernel.tools.shadow import SHADOW_EVIDENCE, ExecutionMode
from biz_kernel.workflows import runner
from biz_kernel.workflows.models import SopRun, SopStep
from biz_port_fakes import FAKES, FakeModel
from biz_port_model_ollama import OllamaModel
from dbos import DBOS
from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/scenarios/pilot_receptionist/smoke.yaml"
HOST = "127.0.0.1"
PORT = 11434
ENDPOINT = f"http://{HOST}:{PORT}"
SENDER = "prospect@example.invalid"


@dataclass(frozen=True)
class Scenario:
    subject: str
    body: str
    category: Literal["enquiry", "service_request"]
    ingress: Literal["form", "mail", "duplicate_mail"]


def load_cases() -> dict[str, Scenario]:
    data = yaml.safe_load(FIXTURES.read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not data.get("cases"):
        raise ValueError("expected nonempty version 1 scenario fixtures")
    cases = {name: Scenario(**case) for name, case in data["cases"].items()}
    for name, case in cases.items():
        if (
            case.category not in ("enquiry", "service_request")
            or case.ingress not in ("form", "mail", "duplicate_mail")
            or not case.subject
            or not case.body
        ):
            raise ValueError(f"invalid pilot receptionist case: {name}")
    return cases


PORTS = {
    "mail": "fake_mail",
    "telephony": "fake_telephony",
    "calendar": "fake_calendar",
    "accounting": "fake_accounting",
    "sign": "fake_sign",
    "storage": "fake_storage",
    "speech": "fake_speech",
}
WRITES = {
    "mail": ("send",),
    "telephony": ("send_sms",),
    "calendar": ("book",),
    "accounting": ("upsert_contact", "create_invoice", "create_bill"),
    "sign": ("request_signature",),
}


class MeteredModel(OllamaModel):
    def __init__(self, model: str, timeout: float) -> None:
        super().__init__(ENDPOINT, model, timeout=timeout)
        self.calls: list[dict[str, Any]] = []

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        call: dict[str, Any] = {
            "path": path,
            "prompt_sha256": hashlib.sha256(
                json.dumps(body.get("messages"), sort_keys=True).encode()
            ).hexdigest(),
            "schema_sha256": hashlib.sha256(
                json.dumps(body.get("format"), sort_keys=True).encode()
            ).hexdigest(),
        }
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=self.timeout) as client:
                response = await client.post(ENDPOINT + path, json=body)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
            call.update(
                input_tokens=result.get("prompt_eval_count"), output_tokens=result.get("eval_count")
            )
            return result
        except Exception as exc:
            call["error"] = str(exc)
            raise
        finally:
            call["latency_ms"] = round((time.monotonic() - start) * 1000)
            self.calls.append(call)


def stop_unexpected_connections(stack: ExitStack, allow_model: bool) -> None:
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def guarded(
            sock: socket.socket, address: Any, original: Callable[..., Any] = original
        ) -> Any:
            if sock.family == socket.AF_UNIX or (allow_model and address == (HOST, PORT)):
                return original(sock, address)
            raise RuntimeError(f"blocked non-Ollama TCP connection: {address}")

        stack.enter_context(patch.object(socket.socket, method, guarded))


def forbid_unconfigured_port(name: str) -> None:
    raise RuntimeError(f"unconfigured test port: {name}")


def complete(sop: str, packet_id: UUID, timeout: float) -> dict[str, Any]:
    run_id = runner.run_id_for("receptionist", sop, packet_id)
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        with db.session_scope() as session:
            current = session.get(SopRun, run_id)
            if current and current.finished_at:
                steps = session.scalars(select(SopStep).where(SopStep.run_id == run_id)).all()
                return {
                    "id": run_id,
                    "status": current.status,
                    "task_id": str(current.task_id),
                    "steps": {row.step_id: row.output for row in steps},
                }
        time.sleep(0.2)
    raise TimeoutError(f"{sop} did not finish")


def packet_ids(kind: str) -> list[UUID]:
    with db.session_scope() as session:
        return list(session.scalars(select(Packet.id).where(Packet.type == kind)))


def write_report(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix=".scenario-")
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, default=str)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def execute(
    case_id: str,
    spec: Scenario,
    repetition: int,
    commit: str,
    model_name: str | None,
    timeout: float,
) -> dict[str, Any]:
    subject, body, category, ingress = spec.subject, spec.body, spec.category, spec.ingress
    checks: dict[str, bool] = {}

    def check(label: str, condition: Any) -> None:
        checks[label] = bool(condition)

    result: dict[str, Any] = {"case": case_id, "repetition": repetition, "checks": checks}
    model = (
        MeteredModel(model_name, timeout)
        if model_name
        else FakeModel(
            [
                {"category": "enquiry", "summary": "Fictional quote request", "confidence": 0.95},
                {
                    "subject": "Site visit",
                    "body": "Which of these times suits you?",
                    "confidence": 0.95,
                },
            ]
        )
    )
    if isinstance(model, MeteredModel):
        result["model_calls"] = model.calls
    spies: dict[str, AsyncMock] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="biz-plan-probe-") as name, ExitStack() as stack:
            pg = pgserver.get_server(Path(name) / "postgres", cleanup_mode="delete")
            app_url = pg.get_uri("application").replace("postgresql://", "postgresql+psycopg://", 1)
            system_url = pg.get_uri("workflow").replace("postgresql://", "postgresql+psycopg://", 1)
            stack.callback(pg.cleanup)
            pg.psql("CREATE DATABASE application;")
            pg.psql("CREATE DATABASE workflow;")
            engine = db.configure(app_url)
            stack.callback(engine.dispose)
            migrations.upgrade(app_url)
            stop_unexpected_connections(stack, allow_model=model_name is not None)
            stack.enter_context(patch.dict(os.environ, {"BIZ_EXECUTION_MODE": "shadow"}))
            stack.enter_context(patch("biz_kernel.runtime.resolve_port", forbid_unconfigured_port))
            ports: dict[str, Any] = {slot: FAKES[backend]() for slot, backend in PORTS.items()}
            ports["model_local"] = model
            for slot, methods in WRITES.items():
                for method in methods:
                    spy = AsyncMock(wraps=getattr(ports[slot], method))
                    stack.enter_context(patch.object(ports[slot], method, spy))
                    spies[f"{slot}.{method}"] = spy

            rt = build_runtime(
                ROOT / "apps/hive", ROOT, ports=ports, approval_poll_seconds=1, retry_seconds=1
            )
            check("shadow gateway", rt.registry.execution_mode == ExecutionMode.SHADOW)
            api = create_app(rt.manifest)
            for _, handler in capability_routes(rt):
                api.include_router(handler, prefix="/webhooks")
            DBOS(
                config={
                    "name": "biz-operator-probe",
                    "system_database_url": system_url,
                    "application_version": commit,
                    "enable_otlp": False,
                }
            )
            stack.callback(DBOS.destroy, destroy_registry=False, workflow_completion_timeout_sec=10)
            DBOS.launch()
            try:
                if ingress == "form":
                    with TestClient(api) as client:
                        response = client.post(
                            "/webhooks/form",
                            json={"email": SENDER, "subject": subject, "message": body},
                        )
                    check("accepted by form ingress", response.status_code == 202)
                else:
                    for _ in range(2 if ingress == "duplicate_mail" else 1):
                        ports["mail"].inject_inbound(
                            InboundMessage(
                                provider_id="fictional-fixture",
                                sender=SENDER,
                                to=["office@example.invalid"],
                                subject=subject,
                                body=body,
                                received_at=datetime.now(UTC),
                            )
                        )
                rt.tick()
                received = packet_ids("enquiry.received")
                check("one intake packet", len(received) == 1)
                if received:
                    triage = complete("triage", received[0], timeout + 20)
                    result["triage"] = triage
                    check("triage complete", triage["status"] == "done")
                    check(
                        "model category",
                        (triage["steps"].get("model") or {}).get("category") == category,
                    )
                opp = packet_ids("opportunity.qualified")
                check("opportunity count", len(opp) == int(category == "enquiry"))
                check(
                    "service request count",
                    len(packet_ids("service_request.received"))
                    == int(category == "service_request"),
                )
                if category == "service_request":
                    check("warranty ambiguity escalated", bool(packet_ids("escalation.raised")))
                if opp:
                    rt.tick()
                    offer = complete("book_meeting", opp[0], timeout + 20)
                    result["offer"] = offer
                    check("offer suppressed", offer["status"] == "shadowed")
                    with db.session_scope() as session:
                        drafts = session.scalars(select(Outbox)).all()
                        check(
                            "only original fictional recipient",
                            len(drafts) == 1
                            and drafts[0].to == SENDER
                            and drafts[0].status != "sent",
                        )
                        check(
                            "no staged dispatch", not session.scalars(select(StagedOperation)).all()
                        )
                        evidence = session.scalars(
                            select(EvidenceEvent).where(EvidenceEvent.kind == SHADOW_EVIDENCE)
                        ).all()
                        audit = (
                            session.get(AuditEntry, UUID(evidence[0].ref))
                            if len(evidence) == 1 and evidence[0].ref
                            else None
                        )
                        check(
                            "suppression audit linked",
                            audit is not None
                            and audit.subject == "comms.send"
                            and audit.detail["recipients"] == [SENDER],
                        )
                        result["suppression_audit_id"] = str(audit.id) if audit else None
                with db.session_scope() as session:
                    enquiries = session.scalars(select(Enquiry)).all()
                    check("exactly one durable enquiry", len(enquiries) == 1)
                    check(
                        "effective category",
                        len(enquiries) == 1 and enquiries[0].classified_as == category,
                    )
                check(
                    "no success packets for outside actions",
                    not packet_ids("message.sent") and not packet_ids("meeting.booked"),
                )
            except TimeoutError as exc:
                result["error"] = str(exc)
                check("workflow completed before timeout", False)
    except Exception as exc:
        result["error"] = repr(exc)
        check("attempt completed", False)
    else:
        result["cleanup"] = "completed"

    ledgers = {name: spy.await_count for name, spy in spies.items()}
    result["fake_external_write_invocations"] = ledgers
    if "error" in result:
        result["ledger_status"] = "provisional"
    else:
        result["ledger_status"] = "observed after workflow and runtime shutdown"
        check("zero fake external write invocations", not any(ledgers.values()))
    result["status"] = "PASS" if all(checks.values()) else "FAIL"
    return result


def main() -> int:
    cases = load_cases()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="installed loopback Ollama tag")
    parser.add_argument("--digest", help="exact Ollama manifest SHA-256")
    parser.add_argument(
        "--fixture-only", action="store_true", help="R01 with deterministic fake inference"
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--case", choices=tuple(cases), action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    selected = args.case or (["R01"] if args.fixture_only else list(cases))
    if args.repetitions < 1 or not 0 < args.timeout <= 600:
        parser.error("use positive repetitions and a timeout in (0, 600]")
    if args.dry_run:
        print("\n".join(selected))
        return 0
    if args.out is None:
        parser.error("--out is required")
    if args.fixture_only and (args.model or args.digest or selected != ["R01"]):
        parser.error("--fixture-only runs R01, without Ollama settings")
    if not args.fixture_only and (not args.model or not args.digest):
        parser.error("supply the installed --model and its exact --digest")

    output = args.out.expanduser().resolve()
    if any((parent / ".git").exists() for parent in output.parents):
        parser.error("--out must be outside Git working trees")
    commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    data: dict[str, Any] = {
        "commit": commit,
        "evidence": "fixture assurance" if args.fixture_only else "actual-model DBOS workflow",
        "model": args.model,
        "digest": args.digest,
        "fixture_sha256": hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
        "endpoint": None if args.fixture_only else ENDPOINT,
        "execution": "shadow",
        "business_ports": PORTS,
        "timeout_seconds": args.timeout,
        "results": [],
    }
    if not args.fixture_only:
        try:
            with httpx.Client(trust_env=False, timeout=5) as client:
                tags = client.get(ENDPOINT + "/api/tags")
                tags.raise_for_status()
                installed = {row["name"]: row for row in tags.json()["models"]}
                entry = installed.get(args.model)
                if not entry or entry["digest"] != args.digest:
                    raise ValueError("model digest does not match the local installation")
                if (
                    entry.get("remote_host")
                    or entry.get("remote_model")
                    or "cloud" in args.model.lower()
                ):
                    raise ValueError("remote model routes are not supported")
                version = client.get(ENDPOINT + "/api/version")
                version.raise_for_status()
                data["ollama"] = version.json()["version"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            data["results"] = [
                {"case": case, "status": "BLOCKED", "reason": str(exc)} for case in selected
            ]
            write_report(output, data)
            print(f"BLOCKED — local model unavailable: {exc}")
            return 2

    for rep in range(1, args.repetitions + 1):
        for case_id in selected:
            result = execute(case_id, cases[case_id], rep, commit, args.model, args.timeout)
            data["results"].append(result)
            write_report(output, data)
            failures = [label for label, passed in result.get("checks", {}).items() if not passed]
            print(case_id, rep, result["status"], *failures, flush=True)
            if result.get("ledger_status") == "provisional":
                print("Stopping after an incomplete attempt. Remaining cases are unverified.")
                return 1
    return int(any(result["status"] != "PASS" for result in data["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
