"""A SOP step that fails never dies silently: a passing fault is retried, and a run that
cannot go on ends as `failed` with an alert and its task left open (never stuck `running`)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select


def _wait(pred, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


@pytest.fixture
def world(fresh_db: str):  # type: ignore[no-untyped-def]
    from biz_kernel import db
    from biz_kernel.evidence import service as evidence
    from biz_kernel.models.router import Router
    from biz_kernel.models_registry import import_all_models
    from biz_kernel.packets.bus import Bus, Packet, SchemaRegistry
    from biz_kernel.policy.engine import PolicyEngine
    from biz_kernel.policy.types import SideEffect
    from biz_kernel.records.models import Note
    from biz_kernel.tools.registry import Tool, ToolRegistry
    from biz_kernel.workflows import runner
    from biz_kernel.workflows.gate import KernelGate
    from dbos import DBOS
    from sqlalchemy import create_engine, make_url

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)

    faults: dict[str, Any] = {"left": 0, "error": ConnectionError("provider is down")}
    reg = ToolRegistry()

    def flaky(session, **_):  # type: ignore[no-untyped-def]
        if faults["left"] > 0:
            faults["left"] -= 1
            raise faults["error"]
        session.add(Note(author="t.flaky", body="ran", source="system"))
        return {"ok": True}

    reg.register(Tool("t.flaky", "t", SideEffect.READ, {"type": "object"}, flaky))

    class Evidence:
        def note(self, session, kind, ref, *, task_id, job_id):  # type: ignore[no-untyped-def]
            evidence.note(session, kind, ref, job_id=job_id, task_id=task_id)

        def complete(self, session, task_id):  # type: ignore[no-untyped-def]
            evidence.complete_task(session, task_id, {"s": ["packet:demo.done"]})

    schemas = SchemaRegistry()
    schemas.register("demo.ping", 1, {"type": "object"})
    schemas.register("demo.done", 1, {"type": "object"})
    sop = {
        "name": "s",
        "trigger": "demo.ping",
        "steps": [
            {"id": "work", "kind": "tool", "tool": "t.flaky", "args": {}},
            {"id": "out", "kind": "publish", "packet": "demo.done", "payload": {}},
        ],
    }
    runner.install(
        runner.RunnerContext(
            bus=Bus(schemas),
            router=Router(local=None),  # type: ignore[arg-type]
            gate=KernelGate(reg, PolicyEngine({}, reg)),
            evidence=Evidence(),
            roles={"r": runner.RoleSpec("r", {"t.flaky"}, {"s": sop}, Path("."))},
            retry_seconds=0.05,
        )
    )
    with db.session_scope() as s:
        p = Packet(type="demo.ping", schema_version=1, from_part="test", payload={})
        s.add(p)
        s.flush()
        packet_id = p.id
    DBOS(config={"name": "biz-failed", "system_database_url": sys_url, "application_version": "t"})
    DBOS.launch()
    try:
        yield faults, packet_id
    finally:
        DBOS.destroy(destroy_registry=False)
        eng.dispose()


def _state(run_id: str) -> tuple[str, str, int, list[str]]:
    from biz_kernel import db
    from biz_kernel.health.models import Alert
    from biz_kernel.records.models import Note, Task
    from biz_kernel.workflows.models import SopRun

    with db.session_scope() as s:
        run = s.get(SopRun, run_id)
        assert run is not None and run.task_id is not None
        task = s.get(Task, run.task_id)
        assert task is not None
        notes = s.scalar(select(func.count()).select_from(Note)) or 0
        alerts = [a.subject for a in s.scalars(select(Alert).where(Alert.kind == "sop.failed"))]
        return run.status, task.status, notes, alerts


def test_a_passing_fault_is_retried_and_the_run_finishes(world) -> None:  # type: ignore[no-untyped-def]
    from biz_kernel.workflows import runner
    from dbos import DBOS

    faults, packet_id = world
    faults["left"] = 2
    (run_id,) = runner.start_for_packet("r", packet_id, "demo.ping")
    result: Any = DBOS.retrieve_workflow(run_id).get_result()
    assert result["status"] == "done"
    assert _state(run_id) == ("done", "done", 1, [])  # the tool's effect happened once


def test_a_run_that_keeps_failing_ends_failed_with_an_alert(world) -> None:  # type: ignore[no-untyped-def]
    from biz_kernel.workflows import runner
    from dbos import DBOS

    faults, packet_id = world
    faults["left"] = 99
    (run_id,) = runner.start_for_packet("r", packet_id, "demo.ping")
    result: Any = DBOS.retrieve_workflow(run_id).get_result()
    assert result["status"] == "failed"
    status, task, notes, alerts = _state(run_id)
    assert (status, task, notes) == ("failed", "open", 0)  # never stuck "running"
    assert alerts == [run_id]  # the owner is told, once
    assert faults["left"] == 99 - runner.MAX_ATTEMPTS  # tried a bounded number of times


def test_a_fault_that_cannot_pass_is_not_retried(world) -> None:  # type: ignore[no-untyped-def]
    from biz_kernel.workflows import runner
    from dbos import DBOS

    faults, packet_id = world
    faults["left"], faults["error"] = 99, PermissionError("not allowed")
    (run_id,) = runner.start_for_packet("r", packet_id, "demo.ping")
    result: Any = DBOS.retrieve_workflow(run_id).get_result()
    assert result["status"] == "failed"
    assert faults["left"] == 98  # one try: a refusal does not change by asking again
    assert _state(run_id)[3] == [run_id]
