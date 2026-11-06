"""SOP runner: every SOP run is a DBOS workflow; every step checkpoints and is safe to resume.

A step's side effects and its ledger row (sop_steps) commit in one transaction, so a step that
crashes after committing returns its stored output when DBOS replays it. Sends inside tools
must also be idempotent (each tool call gets a stable idempotency key).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dbos import DBOS, SetWorkflowID
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.audit.log import record
from biz_kernel.models.router import Router, fence
from biz_kernel.packets.bus import Bus, Packet
from biz_kernel.records.models import Task
from biz_kernel.workflows.models import SopRun, SopStep
from biz_kernel.workflows.templating import render


class Gate(Protocol):
    """Bridge to tools + policy + approvals. Implemented in workflows/gate.py."""

    def call_or_stage(
        self,
        session: Session,
        *,
        actor: str,
        tool: str,
        args: dict[str, Any],
        allow_list: set[str],
        decision_type: str | None,
    ) -> dict[str, Any]:
        """Return {"status": "ran", "output": ...} or {"status": "staged", "op_id": "..."}."""
        ...

    def request(
        self, session: Session, *, actor: str, approver: str, reason: str, subject: dict[str, Any]
    ) -> str:
        """Stage an explicit approval with no tool attached; returns op id."""
        ...

    def execute(self, session: Session, op_id: str) -> Any: ...


class EvidenceHook(Protocol):
    def note(
        self,
        session: Session,
        kind: str,
        ref: str,
        *,
        task_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
    ) -> None: ...

    def complete(self, session: Session, task_id: uuid.UUID) -> None: ...


@dataclass
class RoleSpec:
    name: str
    allow_list: set[str]
    sops: dict[str, dict[str, Any]]  # sop name -> parsed SOP
    pack_dir: Path


@dataclass
class RunnerContext:
    bus: Bus
    router: Router
    gate: Gate
    evidence: EvidenceHook
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    approval_poll_seconds: float = 3600.0


_CTX: RunnerContext | None = None


def install(ctx: RunnerContext) -> None:
    global _CTX
    _CTX = ctx


def ctx() -> RunnerContext:
    if _CTX is None:
        raise RuntimeError("SOP runner not installed; call workflows.runner.install()")
    return _CTX


def run_id_for(role: str, sop: str, packet_id: uuid.UUID | str) -> str:
    return f"{role}:{sop}:{packet_id}"


def sops_for_packet(role: str, packet_type: str) -> list[str]:
    spec = ctx().roles[role]
    return [name for name, sop in spec.sops.items() if sop.get("trigger") == packet_type]


def start_for_packet(role: str, packet_id: uuid.UUID, packet_type: str) -> list[str]:
    """Start every SOP of `role` triggered by this packet. Workflow ids dedupe repeats."""
    started = []
    for sop in sops_for_packet(role, packet_type):
        wf_id = run_id_for(role, sop, packet_id)
        with SetWorkflowID(wf_id):
            DBOS.start_workflow(sop_workflow, role, sop, str(packet_id))
        started.append(wf_id)
    return started


def notify_decision(op_id: str, decision: str) -> bool:
    """Called when a staged operation is approved or rejected; wakes the waiting SOP run."""
    with db.session_scope() as s:
        run = s.query(SopRun).filter_by(waiting_op_id=op_id).one_or_none()
        if run is None:
            return False
        run_id = run.id
    DBOS.send(run_id, decision, topic=f"op:{op_id}", idempotency_key=f"{op_id}:{decision}")
    return True


# ---------------------------------------------------------------- workflow


@DBOS.workflow(name="biz.sop")
def sop_workflow(role: str, sop_name: str, packet_id: str) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    assert run_id is not None
    state = _begin(run_id, role, sop_name, packet_id)
    sop = ctx().roles[role].sops[sop_name]
    for step in sop["steps"]:
        kind = step["kind"]
        if kind == "tool":
            res = _tool_step(run_id, role, step, state)
            if res["status"] == "staged":
                if _await_decision(run_id, res["op_id"]) != "approved":
                    return _end(run_id, "rejected")
                res = {"output": _execute_staged(run_id, step["id"], res["op_id"])}
            state["steps"][step["id"]] = {"output": res["output"]}
        elif kind == "approve":
            op_id = _approval_step(run_id, role, step, state)
            if _await_decision(run_id, op_id) != "approved":
                return _end(run_id, "rejected")
            _mark_approved(run_id, step["id"], op_id)
            state["steps"][step["id"]] = {"output": {"approved": True}}
        elif kind == "draft":
            state["steps"][step["id"]] = {"output": _draft_step(run_id, role, step, state)}
        elif kind == "publish":
            state["steps"][step["id"]] = {"output": _publish_step(run_id, role, step, state)}
        else:
            raise ValueError(f"unknown step kind {kind}")
    return _end(run_id, "done")


def _await_decision(run_id: str, op_id: str) -> str:
    _set_waiting(run_id, op_id)
    while True:
        msg = DBOS.recv(topic=f"op:{op_id}", timeout_seconds=ctx().approval_poll_seconds)
        if msg is not None:
            _set_waiting(run_id, None)
            return str(msg)


# ---------------------------------------------------------------- steps


def _ledger(s: Session, run_id: str, step_id: str) -> SopStep | None:
    return s.get(SopStep, (run_id, step_id))


def _save(s: Session, run_id: str, step_id: str, output: Any) -> Any:
    s.add(SopStep(run_id=run_id, step_id=step_id, output=output))
    return output


@DBOS.step(name="biz.sop.begin")
def _begin(run_id: str, role: str, sop_name: str, packet_id: str) -> dict[str, Any]:
    sop = ctx().roles[role].sops[sop_name]
    with db.session_scope() as s:
        packet = s.get(Packet, uuid.UUID(packet_id))
        if packet is None:
            raise LookupError(f"packet {packet_id} not found")
        pkt = {
            "id": str(packet.id),
            "type": packet.type,
            "summary": packet.summary,
            "payload": packet.payload,
            "job_id": str(packet.job_id or ""),
        }
        run = s.get(SopRun, run_id)
        if run is None:
            task_spec = sop.get("task", {})
            task = Task(
                job_id=packet.job_id,
                kind=task_spec.get("kind", sop_name),
                title=str(render(task_spec.get("title", sop_name), {"packet": pkt})),
                goal_key=sop.get("goal_key"),
                assignee=role,
                source_packet_id=packet.id,
            )
            s.add(task)
            s.flush()
            run = SopRun(
                id=run_id,
                role=role,
                sop=sop_name,
                packet_id=packet.id,
                job_id=packet.job_id,
                task_id=task.id,
                goal_key=sop.get("goal_key"),
            )
            s.add(run)
            record(s, role, "sop.start", sop_name, run_id=run_id, packet_id=packet.id)
        return {
            "packet": pkt,
            "steps": {},
            "task_id": str(run.task_id),
            "goal_key": run.goal_key,
            "sop": sop_name,
        }


@DBOS.step(name="biz.sop.tool")
def _tool_step(
    run_id: str, role: str, step: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step["id"])
        if done is not None:
            return done.output
        args = render(step.get("args", {}), state)
        args = _with_idempotency(args, f"{run_id}:{step['id']}", step["tool"])
        res = c.gate.call_or_stage(
            s,
            actor=role,
            tool=step["tool"],
            args=args,
            allow_list=c.roles[role].allow_list,
            decision_type=step.get("decision_type"),
        )
        if res["status"] == "staged":
            return res  # not ledgered: the execute step owns the outcome
        return _save(s, run_id, step["id"], {"status": "ran", "output": res["output"]})


@DBOS.step(name="biz.sop.execute")
def _execute_staged(run_id: str, step_id: str, op_id: str) -> Any:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step_id)
        if done is not None:
            return done.output["output"]
        output = c.gate.execute(s, op_id)
        _note_approval(s, run_id, op_id)
        _save(s, run_id, step_id, {"status": "ran", "output": output})
        return output


@DBOS.step(name="biz.sop.approval")
def _approval_step(run_id: str, role: str, step: dict[str, Any], state: dict[str, Any]) -> str:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, f"{step['id']}:request")
        if done is not None:
            return str(done.output)
        op_id = c.gate.request(
            s,
            actor=role,
            approver=step.get("approver", "owner"),
            reason=str(render(step.get("reason", ""), state)),
            subject=render(step.get("subject", {}), state),
        )
        return str(_save(s, run_id, f"{step['id']}:request", op_id))


@DBOS.step(name="biz.sop.approved")
def _mark_approved(run_id: str, step_id: str, op_id: str) -> None:
    with db.session_scope() as s:
        if _ledger(s, run_id, step_id) is None:
            _note_approval(s, run_id, op_id)
            _save(s, run_id, step_id, {"approved": True})


@DBOS.step(name="biz.sop.draft")
def _draft_step(run_id: str, role: str, step: dict[str, Any], state: dict[str, Any]) -> Any:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step["id"])
        if done is not None:
            return done.output
    spec = c.roles[role]
    system = (spec.pack_dir / step["prompt"]).read_text()
    inputs = render(step.get("inputs", {}), state)
    # Drafting gets no tools, and every input is untrusted data.
    messages = [{"role": "user", "content": fence(_as_text(inputs))}]
    task = step.get("task", f"{role}.{state.get('sop', step['id'])}")
    result = asyncio.run(c.router.generate(task, system, messages, step["schema"]))
    with db.session_scope() as s:
        if _ledger(s, run_id, step["id"]) is None:
            _save(s, run_id, step["id"], result.output)
            record(s, role, "draft", task, run_id=run_id, tier=result.tier)
    return result.output


@DBOS.step(name="biz.sop.publish")
def _publish_step(run_id: str, role: str, step: dict[str, Any], state: dict[str, Any]) -> Any:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step["id"])
        if done is not None:
            return done.output
        run = s.get(SopRun, run_id)
        assert run is not None
        packet = c.bus.publish(
            s,
            type=step["packet"],
            payload=render(step.get("payload", {}), state),
            from_part=role,
            from_sop=run.sop,
            job_id=run.job_id,
            summary=str(render(step.get("summary", ""), state)),
            goal_key=run.goal_key,
            idempotency_key=f"{run_id}:{step['id']}",
        )
        c.evidence.note(
            s, f"packet:{packet.type}", str(packet.id), task_id=run.task_id, job_id=run.job_id
        )
        return _save(s, run_id, step["id"], {"packet_id": str(packet.id)})


@DBOS.step(name="biz.sop.end")
def _end(run_id: str, status: str) -> dict[str, Any]:
    c = ctx()
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None
        if run.status in ("done", "rejected"):
            return {"run_id": run_id, "status": run.status}
        if status == "done" and run.task_id is not None:
            c.evidence.complete(s, run.task_id)  # raises if declared evidence is missing
        run.status = status
        run.finished_at = db.utcnow()
        record(s, run.role, "sop.end", run.sop, run_id=run_id, status=status)
        return {"run_id": run_id, "status": status}


@DBOS.step(name="biz.sop.waiting")
def _set_waiting(run_id: str, op_id: str | None) -> None:
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None
        run.waiting_op_id = op_id
        run.status = "waiting" if op_id else "running"


def _note_approval(s: Session, run_id: str, op_id: str) -> None:
    run = s.get(SopRun, run_id)
    assert run is not None
    ctx().evidence.note(s, "approval.granted", op_id, task_id=run.task_id, job_id=run.job_id)


def _with_idempotency(args: Any, key: str, tool: str) -> Any:
    accepts = getattr(ctx().gate, "accepts_idempotency", None)
    if isinstance(args, dict) and accepts is not None and accepts(tool):
        return {**args, "idempotency_key": key}
    return args


def _as_text(value: Any) -> str:
    import json

    return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
