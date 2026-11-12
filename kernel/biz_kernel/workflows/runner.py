"""SOP runner: every SOP run is a DBOS workflow; every step checkpoints and is safe to resume.

A step's side effects and its ledger row (sop_steps) commit in one transaction, so a step that
crashes after committing returns its stored output when DBOS replays it. Sends inside tools
must also be idempotent (each tool call gets a stable idempotency key).
"""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dbos import DBOS, SetWorkflowID
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.audit.log import record
from biz_kernel.budgets.service import BudgetBook, BudgetExceeded
from biz_kernel.control import service as control
from biz_kernel.control.service import Paused
from biz_kernel.health.service import raise_alert
from biz_kernel.heartbeats.service import BUDGET_RESUME  # wake-up reason: resume a stopped run
from biz_kernel.models.router import Router
from biz_kernel.packets.bus import Bus, Packet
from biz_kernel.records.models import Task
from biz_kernel.workflows.escalation import CONFIDENCE, Condition, check
from biz_kernel.workflows.models import SopRun, SopStep
from biz_kernel.workflows.templating import TemplateError, holds, render


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
    escalate_when: tuple[Condition, ...] = ()


@dataclass
class RunnerContext:
    bus: Bus
    router: Router
    gate: Gate
    evidence: EvidenceHook
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    approval_poll_seconds: float = 3600.0
    budgets: BudgetBook | None = None
    retry_seconds: float = 60.0  # wait before the first retry of a failed step


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
        if not _applies(role, sop, packet_id):
            continue
        wf_id = run_id_for(role, sop, packet_id)
        with SetWorkflowID(wf_id):
            DBOS.start_workflow(sop_workflow, role, sop, str(packet_id))
        started.append(wf_id)
    return started


def _applies(role: str, sop_name: str, packet_id: uuid.UUID) -> bool:
    """A SOP's optional top-level `when` is checked against the triggering packet."""
    cond = ctx().roles[role].sops[sop_name].get("when")
    if not cond:
        return True
    with db.session_scope() as s:
        packet = s.get(Packet, packet_id)
        if packet is None:
            return False
        pkt = {"type": packet.type, "summary": packet.summary, "payload": packet.payload}
    try:
        return holds(cond, {"packet": pkt})
    except TemplateError:  # the packet lacks the field the condition reads
        return False


def notify_decision(op_id: str, decision: str, client: Any | None = None) -> bool:
    """Called when a staged operation is approved or rejected; wakes the waiting SOP run.

    Pass a `dbos.DBOSClient` (or set module attribute `_CLIENT`) when this process has not
    launched DBOS, as in the API process.
    """
    client = client or globals().get("_CLIENT")
    from biz_kernel.approvals.models import StagedOperation

    with db.session_scope() as s:
        run = s.query(SopRun).filter_by(waiting_op_id=op_id).one_or_none()
        if run is None:
            return False
        run_id = run.id
        op = s.get(StagedOperation, uuid.UUID(op_id))
        version = op.version if op is not None else 0
    key = f"{op_id}:{decision}:v{version}"
    if client is not None:
        client.send(run_id, decision, topic=f"op:{op_id}", idempotency_key=key)
    else:
        DBOS.send(run_id, decision, topic=f"op:{op_id}", idempotency_key=key)
    return True


# ---------------------------------------------------------------- workflow


MAX_ATTEMPTS = 4  # tries of one step before the run ends as failed
RETRY_BACKOFF = (1, 10, 60)  # multiples of retry_seconds: 1 min, 10 min, 1 hour by default
FAILED_ALERT = "sop.failed"


class StepFailed(Exception):
    """A step raised. Carries only text, so DBOS can always store and replay it.

    `permanent` marks a refusal or a bad input that asking again cannot change."""

    def __init__(self, error: str, permanent: bool = False) -> None:
        super().__init__(error)
        self.error, self.permanent = error, permanent

    def __reduce__(self) -> Any:
        return (StepFailed, (self.error, self.permanent))


class _RunFailed(Exception):
    """The run was marked failed; the workflow just has to return."""


def _is_permanent(exc: Exception) -> bool:
    """Refusals, missing evidence and bad inputs stay wrong however often they are tried."""
    from biz_kernel.evidence.service import EvidenceMissing

    return isinstance(exc, PermissionError | LookupError | ValueError | TypeError | EvidenceMissing)


def _checked(fn: Any) -> Any:
    """Turn whatever a step raises into StepFailed (budget stops pass through)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (BudgetExceeded, Paused, StepFailed):
            raise
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"[:500]
            raise StepFailed(text, _is_permanent(exc)) from None

    return wrapper


@DBOS.workflow(name="biz.sop")
def sop_workflow(role: str, sop_name: str, packet_id: str) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    assert run_id is not None
    try:
        return _run(run_id, role, sop_name, packet_id)
    except _RunFailed:
        return {"run_id": run_id, "status": "failed"}
    except (ValueError, LookupError, TypeError) as exc:  # a broken SOP definition
        _step_failed(run_id, role, f"{type(exc).__name__}: {exc}"[:500], True)
        return {"run_id": run_id, "status": "failed"}


def _run(run_id: str, role: str, sop_name: str, packet_id: str) -> dict[str, Any]:
    def do(fn: Any, *args: Any) -> Any:
        return _attempt(run_id, role, functools.partial(fn, *args))

    state = do(_begin, run_id, role, sop_name, packet_id)
    sop = ctx().roles[role].sops[sop_name]
    conds = _conditions_by_step(sop, ctx().roles[role].escalate_when)
    for step in sop["steps"]:
        kind = step["kind"]
        _hold_while_paused(run_id, role)
        if step.get("when") and not _step_applies(step["when"], state):
            state["steps"][step["id"]] = {"output": {"skipped": True}}
            continue
        if kind == "tool":
            res = do(_tool_step, run_id, role, step, state)
            while res["status"] == "staged":
                if _await_decision(run_id, res["op_id"]) != "approved":
                    return do(_end, run_id, "rejected")
                res = do(_execute_staged, run_id, role, step["id"], res["op_id"])
            state["steps"][step["id"]] = {"output": res["output"]}
            if why := check(conds[step["id"]], kind=kind, output=res["output"]):
                return _escalate(run_id, role, state, why)
        elif kind == "approve":
            op_id = do(_approval_step, run_id, role, step, state)
            if _await_decision(run_id, op_id) != "approved":
                return do(_end, run_id, "rejected")
            do(_mark_approved, run_id, step["id"], op_id)
            state["steps"][step["id"]] = {"output": {"approved": True}}
        elif kind == "draft":
            out = do(_draft_step, run_id, role, step, state)
            state["steps"][step["id"]] = {"output": out}
            if why := check(conds[step["id"]], kind=kind, output=out):
                return _escalate(run_id, role, state, why)
        elif kind == "stop":
            # End the run without completing the task: it stays open for the owner.
            return do(_end, run_id, step.get("status", "escalated"))
        elif kind == "publish":
            state["steps"][step["id"]] = {"output": do(_publish_step, run_id, role, step, state)}
        else:
            raise ValueError(f"unknown step kind {kind}")
    return do(_end, run_id, "done")


def _attempt(run_id: str, role: str, step: Any) -> Any:
    """Run a step so that it never dies silently.

    Budget stop: the run is marked budget_stopped (not left "running") and a heartbeat
    wake-up is queued; the heartbeat keeps it pending while the role is stopped, and once
    the limit covers the step it sends the resume message and the step is tried again.

    Any other failure: the step is tried again after a growing wait (every step is safe to
    repeat). When it still fails after MAX_ATTEMPTS, or the failure is one that cannot pass,
    the run ends as failed with an alert, and its task stays open for the owner.
    """
    failures = 0
    while True:
        try:
            return step()
        except Paused:  # the kill switch closed while the step was starting
            _hold_while_paused(run_id, role)
            # The transition can still be uncommitted and invisible to the check.
            DBOS.sleep(0.05)
            continue
        except BudgetExceeded as exc:
            _budget_stop(run_id, role, str(exc.needed))
            DBOS.recv(topic="budget", timeout_seconds=ctx().approval_poll_seconds)
            _set_waiting(run_id, None)  # back to running
        except StepFailed as exc:
            failures += 1
            final = exc.permanent or failures >= MAX_ATTEMPTS
            _step_failed(run_id, role, exc.error, final)
            if final:
                raise _RunFailed(exc.error) from None
            DBOS.sleep(ctx().retry_seconds * RETRY_BACKOFF[failures - 1])


@DBOS.step(name="biz.sop.failed")
def _step_failed(run_id: str, role: str, error: str, final: bool) -> None:
    """Log the failure; a final one marks the run failed and raises the alert (once)."""
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        sop = run.sop if run is not None else run_id
        record(s, role, "sop.step_failed", sop, run_id=run_id, error=error, final=final)
        if not final:
            return
        if run is not None and run.status not in ("done", "rejected"):
            run.status, run.waiting_op_id, run.finished_at = "failed", None, db.utcnow()
        raise_alert(s, FAILED_ALERT, run_id, f"{role} could not finish {sop}: {error}", db.utcnow())


PAUSE_TOPIC = "pause"


def _hold_while_paused(run_id: str, role: str) -> None:
    """Park the run while the kill switch is on. The heartbeat after the resume wakes it."""
    while _pause_check(run_id, role):
        DBOS.recv(topic=PAUSE_TOPIC, timeout_seconds=ctx().approval_poll_seconds)


@DBOS.step(name="biz.sop.pause_check")
def _pause_check(run_id: str, role: str) -> bool:
    with db.session_scope() as s:
        paused = control.is_paused(s)
        run = s.get(SopRun, run_id)
        assert run is not None
        if paused and run.status != "paused":
            run.status = "paused"
            record(s, role, "sop.paused", run.sop, run_id=run_id)
        elif not paused and run.status == "paused":
            run.status = "running"
        return paused


def resume_paused(run_ids: list[str], client: Any | None = None) -> None:
    """Wake runs parked by the kill switch (called once it is off)."""
    client = client or globals().get("_CLIENT")
    for run_id in run_ids:
        if client is not None:
            client.send(run_id, "resume", topic=PAUSE_TOPIC)
        else:
            DBOS.send(run_id, "resume", topic=PAUSE_TOPIC)


def resume_budget_stopped(reasons: list[str], client: Any | None = None) -> list[str]:
    """Wake every budget-stopped run named in these wake-up reasons. Returns their ids."""
    client = client or globals().get("_CLIENT")
    woken = []
    for reason in reasons:
        if reason.startswith(BUDGET_RESUME):
            run_id = reason[len(BUDGET_RESUME) :].split("@", 1)[0]
            if client is not None:
                client.send(run_id, "resume", topic="budget")
            else:
                DBOS.send(run_id, "resume", topic="budget")
            woken.append(run_id)
    return woken


@DBOS.step(name="biz.sop.budget_stop")
def _budget_stop(run_id: str, role: str, needed: str = "0") -> None:
    from sqlalchemy import select

    from biz_kernel.heartbeats.models import Wakeup
    from biz_kernel.heartbeats.service import enqueue

    prefix = f"{BUDGET_RESUME}{run_id}@"
    reason = f"{prefix}{needed}"
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None
        if run.status != "budget_stopped":
            run.status = "budget_stopped"
            record(s, role, "sop.budget_stopped", run.sop, run_id=run_id)
        pending = s.scalar(select(Wakeup).where(Wakeup.role == role, Wakeup.status == "pending"))
        if pending is None or not any(r.startswith(prefix) for r in pending.reasons):
            enqueue(s, role, reason, db.utcnow())


def _picklable(fn: Any) -> Any:
    """DBOS stores step errors by pickling them; schema errors don't pickle, so convert."""
    import functools

    import jsonschema

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"tool arguments rejected: {exc.message}") from None

    return wrapper


def _step_applies(cond: str, state: dict[str, Any]) -> bool:
    """A step `when` that reads a missing field (e.g. a skipped step's output) is false."""
    try:
        return holds(cond, state)
    except TemplateError:
        return False


def _await_decision(run_id: str, op_id: str) -> str:
    """Wait until the operation is approved (at its current version) or rejected.

    Messages are only wake-up hints; the staged operation's row is the truth, so an
    approval that was edited or revoked before we woke is never acted on.
    """
    _set_waiting(run_id, op_id)
    while True:
        state = _op_state(op_id)  # first: a decision made before we started waiting counts
        if state in ("approved", "rejected"):
            _set_waiting(run_id, None)
            return state
        DBOS.recv(topic=f"op:{op_id}", timeout_seconds=ctx().approval_poll_seconds)


@DBOS.step(name="biz.sop.op_state")
def _op_state(op_id: str) -> str:
    from biz_kernel.approvals.models import StagedOperation

    with db.session_scope() as s:
        op = s.get(StagedOperation, uuid.UUID(op_id))
        if op is None or op.status == "rejected":
            return "rejected"
        if op.status in ("approved", "executed") and op.approved_version == op.version:
            return "approved"
        return "pending"


# ---------------------------------------------------------------- steps


def _ledger(s: Session, run_id: str, step_id: str) -> SopStep | None:
    return s.get(SopStep, (run_id, step_id))


def _save(s: Session, run_id: str, step_id: str, output: Any) -> Any:
    s.add(SopStep(run_id=run_id, step_id=step_id, output=output))
    return output


@DBOS.step(name="biz.sop.begin")
@_checked
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
@_checked
def _tool_step(
    run_id: str, role: str, step: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step["id"]) or _ledger(s, run_id, f"{step['id']}:staged")
        if done is not None:
            return done.output
        args = render(step.get("args", {}), state)
        args = _with_idempotency(args, f"{run_id}:{step['id']}", step["tool"])
        res = _picklable(c.gate.call_or_stage)(
            s,
            actor=role,
            tool=step["tool"],
            args=args,
            allow_list=c.roles[role].allow_list,
            decision_type=step.get("decision_type"),
        )
        if res["status"] == "staged":
            return _save(s, run_id, f"{step['id']}:staged", res)  # execute step owns the outcome
        return _save(s, run_id, step["id"], {"status": "ran", "output": res["output"]})


@DBOS.step(name="biz.sop.execute")
@_checked
def _execute_staged(run_id: str, role: str, step_id: str, op_id: str) -> dict[str, Any]:
    """Run the approved operation once. Returns {"status": "ran", ...} or, if the approval
    went stale between the check and the lock, {"status": "staged", ...} to wait again."""
    from biz_kernel.approvals.models import StagedOperation
    from biz_kernel.approvals.staged import NotApproved

    c = ctx()
    with db.session_scope() as s:
        done = _ledger(s, run_id, step_id)
        if done is not None:
            return dict(done.output)
        op = s.get(StagedOperation, uuid.UUID(op_id))
        if op is None or op.tool not in c.roles[role].allow_list:
            raise PermissionError(f"{role} may no longer call {op.tool if op else op_id}")
        try:
            with s.begin_nested():
                output = c.gate.execute(s, op_id)
        except NotApproved:
            return {"status": "staged", "op_id": op_id}
        _note_approval(s, run_id, op_id)
        return dict(_save(s, run_id, step_id, {"status": "ran", "output": output}))


@DBOS.step(name="biz.sop.approval")
@_checked
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
@_checked
def _mark_approved(run_id: str, step_id: str, op_id: str) -> None:
    with db.session_scope() as s:
        if _ledger(s, run_id, step_id) is None:
            _note_approval(s, run_id, op_id)
            _save(s, run_id, step_id, {"approved": True})


@DBOS.step(name="biz.sop.draft")
@_checked
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
    task = step.get("task", f"{role}.{state.get('sop', step['id'])}")
    result = _generate(role, task, system, step["schema"], _as_text(inputs))
    with db.session_scope() as s:
        if _ledger(s, run_id, step["id"]) is None:
            _save(s, run_id, step["id"], result.output)
            record(s, role, "draft", task, run_id=run_id, tier=result.tier)
    return result.output


def _generate(role: str, task: str, system: str, schema: dict[str, Any], text: str) -> Any:
    """One routed model call. What it cost is committed even when the draft is refused.

    The router writes its cost rows into the session it is given. A paid call whose output
    fails validation is still real spend, so the session commits before the failure is
    raised; rolling it back would hide the spend from the role's budget.
    """
    failure: Exception | None = None
    with db.session_scope() as s:
        try:
            return asyncio.run(
                ctx().router.generate(
                    task, system, [], schema, untrusted=[text], session=s, role=role
                )
            )
        except Exception as exc:
            failure = exc
    raise failure


@DBOS.step(name="biz.sop.publish")
@_checked
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
        if step.get("evidence"):
            c.evidence.note(
                s, step["evidence"], str(packet.id), task_id=run.task_id, job_id=run.job_id
            )
        return _save(s, run_id, step["id"], {"packet_id": str(packet.id)})


def _conditions_by_step(
    sop: dict[str, Any], conds: tuple[Condition, ...]
) -> dict[str, tuple[Condition, ...]]:
    """The role's escalate_when conditions that each step is checked against.

    A SOP keeps control of what it handles itself: confidence is skipped for a draft whose
    confidence a later step reads, and signals are skipped in a SOP with its own escalation
    (a stop step or an escalation.raised publish). Everything else is checked.
    """
    text = json.dumps(sop["steps"])
    own = any(s["kind"] == "stop" or s.get("packet") == "escalation.raised" for s in sop["steps"])
    out = {}
    for step in sop["steps"]:
        reads_conf = f"steps.{step['id']}.output.confidence" in text
        out[step["id"]] = tuple(
            c
            for c in conds
            if (c.name == CONFIDENCE and not reads_conf) or (c.name != CONFIDENCE and not own)
        )
    return out


def _escalate(run_id: str, role: str, state: dict[str, Any], why: str) -> dict[str, Any]:
    """An escalate_when condition matched: tell the owner, then end like a stop step."""
    _attempt(run_id, role, functools.partial(_raise_escalation, run_id, role, state["packet"], why))
    return _attempt(run_id, role, functools.partial(_end, run_id, "escalated"))


@DBOS.step(name="biz.sop.escalate")
@_checked
def _raise_escalation(run_id: str, role: str, packet: dict[str, Any], why: str) -> None:
    c = ctx()
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None
        c.bus.publish(
            s,
            type="escalation.raised",
            payload={
                "subject": f"{run.sop} needs the owner: {packet.get('summary', '')}",
                "reason": why,
                "raised_by": role,
                "packet_id": packet["id"],
            },
            from_part=role,
            from_sop=run.sop,
            job_id=run.job_id,
            summary=f"{run.sop} escalated: {why}",
            goal_key=run.goal_key,
            idempotency_key=f"{run_id}:escalate_when",
        )
        record(s, role, "sop.escalate_when", run.sop, run_id=run_id, reason=why)


@DBOS.step(name="biz.sop.end")
@_checked
def _end(run_id: str, status: str) -> dict[str, Any]:
    c = ctx()
    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None
        if run.status in ("done", "rejected", "failed"):
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
