"""Runtime: assemble one composition (ports, tools, policy, bus, runner) from its manifest."""

from __future__ import annotations

import importlib
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.evidence import service as evidence
from biz_kernel.health.models import Schedule
from biz_kernel.health.service import register_schedule
from biz_kernel.heartbeats import service as heartbeats
from biz_kernel.heartbeats.followthrough import FollowThrough
from biz_kernel.loader import Manifest, load_composition, parse_cadence
from biz_kernel.memory import tools as memory_tools
from biz_kernel.models.router import Router
from biz_kernel.packets.bus import Bus, Packet
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.tools.registry import ToolRegistry
from biz_kernel.workflows import runner
from biz_kernel.workflows.escalation import parse
from biz_kernel.workflows.gate import KernelGate
from biz_kernel.workflows.models import SopRun

PACKET_REASON = "packet:"
log = logging.getLogger(__name__)


class CompositionError(RuntimeError):
    pass


def resolve_port(name: str) -> Any:
    """`fake_*` names come from biz_port_fakes; others from package biz_port_<name>."""
    if name.startswith("fake_"):
        from biz_port_fakes import FAKES

        return FAKES[name]()
    mod = importlib.import_module(f"biz_port_{name}")
    return mod.create()


@dataclass
class KernelEvidence:
    rules: evidence.EvidenceRules

    def note(
        self,
        session: Session,
        kind: str,
        ref: str,
        *,
        task_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
    ) -> None:
        evidence.note(session, kind, ref, job_id=job_id, task_id=task_id)

    def complete(self, session: Session, task_id: uuid.UUID) -> None:
        evidence.complete_task(session, task_id, self.rules)


@dataclass
class Runtime:
    manifest: Manifest
    ports: dict[str, Any]
    registry: ToolRegistry
    policy: PolicyEngine
    bus: Bus
    budgets: BudgetBook
    router: Router
    gate: KernelGate
    embedder: Any = None
    follow: FollowThrough = field(default_factory=FollowThrough)
    pollers: list[Callable[[], int]] = field(default_factory=list)

    # -------------------------------------------------------------- memory

    def seed_memory(self) -> int:
        """Ingest the composition's memory_profile folder as cited draft notes (idempotent)."""
        from biz_kernel.memory import load_profile

        folder = self.manifest.app_dir / "memory"
        if not folder.is_dir():
            return 0
        with db.session_scope() as s:
            return len(load_profile(s, folder, self.embedder) or [])

    # -------------------------------------------------------------- heartbeats

    def on_packet(self, role: str):  # type: ignore[no-untyped-def]
        def handler(session: Session, packet: Packet) -> None:
            heartbeats.enqueue(
                session, role, f"{PACKET_REASON}{packet.id}:{packet.type}", db.utcnow()
            )

        return handler

    def run_role(self, role: str, reasons: list[str]) -> None:
        """Called by the heartbeat: start the SOPs triggered by each coalesced packet."""
        for reason in reasons:
            if reason.startswith(PACKET_REASON):
                packet_id, packet_type = reason[len(PACKET_REASON) :].split(":", 1)
                runner.start_for_packet(role, uuid.UUID(packet_id), packet_type)

    def cadences(self) -> dict[str, Any]:
        return {name: parse_cadence(r.heartbeat_cadence) for name, r in self.manifest.roles.items()}

    def poll(self) -> int:
        """Run capability pollers (e.g. pull new mail). One failing poller never blocks the rest."""
        total = 0
        for poller in self.pollers:
            try:
                total += poller()
            except Exception:
                log.exception("poller %s failed", getattr(poller, "__name__", poller))
        return total

    def tick(self, now: datetime | None = None) -> list[str]:
        """One heartbeat cycle; commits so started workflows can see the claimed work."""
        self.poll()  # before reading the clock: wake-ups it enqueues must be due now
        now = now or db.utcnow()
        with db.session_scope() as s:
            for role, every in self.cadences().items():
                name = f"heartbeat:{role}"
                if s.scalar(select(Schedule.id).where(Schedule.name == name)) is None:
                    register_schedule(s, name, every, now)
            heartbeats.schedule_cadence(s, self.cadences(), now)
            return heartbeats.run_heartbeat(s, self.run_role, self.budgets, self.follow.sweep, now)

    # -------------------------------------------------------------- follow-through

    def _register_follow_through(self) -> None:
        def decided_but_waiting(session: Session) -> list[str]:
            """Runs still waiting on an operation that has already been approved or rejected."""
            rows = session.execute(
                select(SopRun.waiting_op_id, StagedOperation.status)
                .join(StagedOperation, StagedOperation.id == _as_uuid(SopRun.waiting_op_id))
                .where(
                    SopRun.status == "waiting", StagedOperation.status.in_(("approved", "rejected"))
                )
            ).all()
            return [f"{op}:{status}" for op, status in rows]

        def resume(session: Session, ref: str) -> None:
            op_id, status = ref.split(":")
            runner.notify_decision(op_id, "approved" if status == "approved" else "rejected")

        self.follow.register("sop_waiting_on_decided_op", decided_but_waiting, resume)


def capability_routes(rt: Runtime, hook: str = "routes") -> list[tuple[str, Any]]:
    """(name, APIRouter) per loaded capability defining `hook(rt)`.

    `routes` are public webhooks (mounted under /webhooks); `owner_routes` are signed-in
    owner/staff endpoints (mounted under /v1, private).
    """
    out = []
    for cap in rt.manifest.capabilities:
        mod = importlib.import_module(f"biz_cap_{cap}")
        if callable(getattr(mod, hook, None)):
            out.append((cap, getattr(mod, hook)(rt)))
    return out


def _as_uuid(col: Any) -> Any:
    from sqlalchemy import Uuid, cast

    return cast(col, Uuid)


def _embedder(model: Any) -> Any:
    """Use the local model's embeddings when it has them; otherwise a deterministic hash."""
    import asyncio
    import inspect

    from biz_kernel.memory import HashEmbedder

    embed = getattr(model, "embed", None)
    if embed is None or not inspect.iscoroutinefunction(embed):
        return HashEmbedder(int(os.environ.get("BIZ_EMBED_DIM", "768")))

    async_embed = embed

    class _Sync:
        def embed(self, texts: list[str]) -> list[list[float]]:
            result: list[list[float]] = asyncio.run(async_embed(texts))
            return result

    return _Sync()


def build_runtime(
    app_dir: Path,
    repo_root: Path,
    ports: dict[str, Any] | None = None,
    approval_poll_seconds: float = 3600.0,
) -> Runtime:
    manifest = load_composition(app_dir, repo_root)
    overrides = ports or {}
    resolved = {
        slot: resolve_port(name) for slot, name in manifest.ports.items() if slot not in overrides
    }
    resolved.update(overrides)

    registry = ToolRegistry()
    embedder = _embedder(resolved.get("model_local"))
    memory_tools.register(
        registry, {name: list(r.sources) for name, r in manifest.roles.items()}, embedder
    )
    for cap in manifest.capabilities:
        mod = importlib.import_module(f"biz_cap_{cap}")
        mod.register(registry, resolved)
    if unsafe := registry.not_idempotent():
        raise CompositionError(f"gated tools must take an idempotency_key: {unsafe}")
    missing = set(manifest.tools) - set(registry.tools) - {"memory.recall"}
    if missing:
        raise CompositionError(f"capabilities declared tools they did not register: {missing}")

    policy = PolicyEngine(manifest.policy, registry)
    bus = Bus(manifest.registry)
    budgets = BudgetBook({r: manifest.role_budget(r) for r in manifest.roles})
    registry.budgets = budgets  # paid tool calls are checked and charged per role
    task_tiers: dict[str, Any] = {}
    for role in manifest.roles.values():
        for topic, tier in role.model_tier.items():
            task_tiers[f"{role.name}.{topic}"] = tier
    router = Router(
        local=resolved["model_local"],
        cloud=resolved.get("model_cloud"),
        task_tiers=task_tiers,
        high_stakes=set(manifest.policy.get("high_stakes") or []),
        budgets=budgets,
    )
    gate = KernelGate(registry, policy)
    rules: evidence.EvidenceRules = {}
    for role in manifest.roles.values():
        rules.update(role.evidence)

    rt = Runtime(manifest, resolved, registry, policy, bus, budgets, router, gate, embedder)
    for name, role in manifest.roles.items():
        for packet_type in set(role.consumes) | set(role.heartbeat_triggers):
            bus.subscribe(packet_type, name, rt.on_packet(name))
    rt._register_follow_through()
    for cap in manifest.capabilities:
        mod = importlib.import_module(f"biz_cap_{cap}")
        if callable(getattr(mod, "pollers", None)):
            rt.pollers.extend(mod.pollers(rt))

    signals = {sig for c in manifest.capabilities.values() for sig in c.signals}
    runner.install(
        runner.RunnerContext(
            bus=bus,
            router=router,
            gate=gate,
            evidence=KernelEvidence(rules),
            approval_poll_seconds=approval_poll_seconds,
            roles={
                name: runner.RoleSpec(
                    name=name,
                    allow_list=set(role.allow_list),
                    sops={t.sop.get("name", t.name): t.sop for t in role.topics},
                    pack_dir=role.path,
                    escalate_when=parse(role.escalate_when, known_signals=signals),
                )
                for name, role in manifest.roles.items()
            },
        )
    )
    return rt
