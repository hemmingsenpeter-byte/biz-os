"""Subprocess worker for test_workflow_resume. argv: db_url sys_db_url mode packet_id marker."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

from biz_kernel import db
from biz_kernel.evidence import service as evidence
from biz_kernel.models.router import Router
from biz_kernel.packets.bus import Bus, SchemaRegistry
from biz_kernel.policy.types import Allow, SideEffect
from biz_kernel.records.models import Note
from biz_kernel.tools.registry import Tool, ToolRegistry
from biz_kernel.workflows import runner
from dbos import DBOS
from sqlalchemy import select

OBJ = {"type": "object"}


def main() -> None:
    url, sys_url, mode, packet_id, marker = sys.argv[1:6]
    db.configure(url)
    reg = ToolRegistry()

    def step_one(session: Any) -> dict[str, Any]:
        session.add(Note(author="t.step_one", body="ran", source="system"))
        return {"n": 1}

    def maybe_crash(session: Any) -> dict[str, Any]:
        if mode == "crash" and not Path(marker).exists():
            Path(marker).write_text("crashed")
            os._exit(17)  # simulate the worker being killed mid-SOP
        return {"ok": True}

    reg.register(Tool("t.step_one", "t", SideEffect.DRAFT, OBJ, step_one))
    reg.register(Tool("t.maybe_crash", "t", SideEffect.DRAFT, OBJ, maybe_crash))

    class Policy:
        def check(self, session: Any, action: Any) -> Allow:
            return Allow()

    class Gate:
        def call_or_stage(
            self,
            session: Any,
            *,
            actor: str,
            tool: str,
            args: dict,
            allow_list: set[str],
            decision_type: str | None,
        ) -> dict:
            res = reg.call(
                session, Policy(), actor=actor, name=tool, args=args, allow_list=allow_list
            )
            return {"status": "ran", "output": res.output}

        def request(self, *a: Any, **k: Any) -> str:
            raise NotImplementedError

        def execute(self, *a: Any, **k: Any) -> Any:
            raise NotImplementedError

    class Evidence:  # the real evidence service: done only with declared evidence
        def note(self, session: Any, kind: str, ref: str, *, task_id: Any, job_id: Any) -> None:
            evidence.note(session, kind, ref, job_id=job_id, task_id=task_id)

        def complete(self, session: Any, task_id: uuid.UUID) -> None:
            evidence.complete_task(session, task_id, {"s": ["packet:demo.done"]})

    schemas = SchemaRegistry()
    schemas.register("demo.ping", 1, OBJ)
    schemas.register("demo.done", 1, OBJ)
    sop = {
        "name": "s",
        "trigger": "demo.ping",
        "steps": [
            {"id": "one", "kind": "tool", "tool": "t.step_one", "args": {}},
            {"id": "two", "kind": "tool", "tool": "t.maybe_crash", "args": {}},
            {
                "id": "three",
                "kind": "publish",
                "packet": "demo.done",
                "payload": {"from": "{{packet.id}}"},
            },
        ],
    }
    runner.install(
        runner.RunnerContext(
            bus=Bus(schemas),
            router=Router(local=None),  # type: ignore[arg-type]
            gate=Gate(),
            evidence=Evidence(),
            roles={
                "r": runner.RoleSpec("r", {"t.step_one", "t.maybe_crash"}, {"s": sop}, Path("."))
            },
        )
    )
    DBOS(
        config={
            "name": "biz-test",
            "system_database_url": sys_url,
            "application_version": "test",
            "executor_id": "local",
        }
    )
    DBOS.launch()  # recovers any workflow left pending by a killed worker
    wf_id = runner.run_id_for("r", "s", packet_id)
    if mode == "crash":
        runner.start_for_packet("r", uuid.UUID(packet_id), "demo.ping")
    result = DBOS.retrieve_workflow(wf_id).get_result()
    with db.session_scope() as s:
        notes = len(list(s.scalars(select(Note).where(Note.author == "t.step_one"))))
    print(f"RESULT {result['status']} notes={notes}")
    DBOS.destroy()


if __name__ == "__main__":
    main()
