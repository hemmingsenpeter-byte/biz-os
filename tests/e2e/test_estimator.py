"""M5: survey -> scope -> estimate -> approval, and the margin-below-policy escalation.

Runs against the composition named by BIZ_ESTIMATOR_APP (default apps/hive) once the quoting and
documents capabilities and the estimator role are wired into it."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
import yaml
from biz_kernel import db
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Job
from biz_kernel.runtime import build_runtime
from biz_kernel.workflows.gate import decide
from biz_kernel.workflows.models import SopRun
from biz_port_fakes import FakeModel, FakeSign, FakeStorage
from dbos import DBOS
from sqlalchemy import create_engine, make_url, select

ROOT = Path(__file__).parents[2]
APP = Path(os.environ.get("BIZ_ESTIMATOR_APP", ROOT / "apps" / "hive"))


def _wired() -> bool:
    try:
        import biz_cap_documents  # noqa: F401
        import biz_cap_quoting  # noqa: F401

        comp = yaml.safe_load((APP / "composition.yaml").read_text())
    except Exception:
        return False
    return "estimator" in comp.get("roles", []) and {"quoting", "documents"} <= set(
        comp.get("capabilities", [])
    )


pytestmark = pytest.mark.skipif(
    not _wired(), reason="needs quoting + documents capabilities wired into compositions"
)

PRICE_ROWS = [
    {
        "code": "PT-01",
        "category": "partitions",
        "description": "Stud partition",
        "unit": "m2",
        "unit_cost": "60",
        "unit_price": "90",
    }
]
SCOPE = {
    "areas": [
        {
            "name": "Open plan",
            "items": [
                {
                    "category": "partitions",
                    "description": "Stud partition",
                    "quantity": 20,
                    "unit": "m2",
                }
            ],
        }
    ],
    "area_names": ["Open plan"],
    "exclusions": ["Furniture"],
    "assumptions": ["Out-of-hours access"],
    "confidence": 0.9,
}


def _wait(pred, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


@pytest.fixture
def rt(fresh_db: str, monkeypatch):  # type: ignore[no-untyped-def]
    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)

    def no_real_ports(name: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"port {name} was not overridden with a fake")

    monkeypatch.setattr("biz_kernel.runtime.resolve_port", no_real_ports)
    manifest_ports = yaml.safe_load((APP / "composition.yaml").read_text())["ports"]
    fakes = {
        "sign": FakeSign(),
        "storage": FakeStorage(),
        "model_local": FakeModel(),
        "model_cloud": FakeModel(),
    }
    ports = {k: fakes.get(k) or _generic(k) for k in manifest_ports}
    runtime = build_runtime(APP, ROOT, ports=ports, approval_poll_seconds=1)
    DBOS(config={"name": "biz-e2e", "system_database_url": sys_url, "application_version": "test"})
    DBOS.launch()
    yield runtime
    DBOS.destroy(destroy_registry=False)
    eng.dispose()


def _generic(name: str):  # type: ignore[no-untyped-def]
    from biz_port_fakes import FakeCalendar, FakeMail, FakeTelephony

    return {"mail": FakeMail, "telephony": FakeTelephony, "calendar": FakeCalendar}[name]()


def _publish(rt, type_: str, payload: dict, job_id: uuid.UUID, summary: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    with db.session_scope() as s:
        return rt.bus.publish(
            s, type=type_, from_part="test", summary=summary, payload=payload, job_id=job_id
        ).id


def _run(sop: str, packet_id: uuid.UUID) -> SopRun:
    def find() -> SopRun | None:
        with db.session_scope() as s:
            run = s.get(SopRun, f"estimator:{sop}:{packet_id}")
            if run is not None:
                s.expunge(run)
            return run

    return _wait(find)


def _approve_next(run: SopRun) -> None:
    from biz_kernel.approvals.models import StagedOperation

    def waiting():  # type: ignore[no-untyped-def]
        with db.session_scope() as s:
            r = s.get(SopRun, run.id)
            if r is None or r.status != "waiting" or not r.waiting_op_id:
                return None
            op = s.get(StagedOperation, uuid.UUID(r.waiting_op_id))
            s.expunge(op)
            return op

    op = _wait(waiting)
    decide(op.id, op.approver, approve=True, version=op.version)


def _packets(type_: str) -> list[Packet]:
    with db.session_scope() as s:
        rows = list(s.scalars(select(Packet).where(Packet.type == type_)))
        s.expunge_all()
        return rows


def _seed(rt) -> uuid.UUID:  # type: ignore[no-untyped-def]
    with db.session_scope() as s:
        job = Job(kind="fitout", stage="open")
        s.add(job)
        s.flush()
        job_id = job.id
    from biz_cap_quoting.models import PriceItem

    with db.session_scope() as s:
        for r in PRICE_ROWS:
            s.add(PriceItem(source="test", **r))
    return job_id


def test_survey_to_scope_to_estimate(rt) -> None:  # type: ignore[no-untyped-def]
    job_id = _seed(rt)
    cloud: FakeModel = rt.ports["model_cloud"]
    cloud.script(
        SCOPE,
        {
            "lines": [
                {
                    "code": "PT-01",
                    "description": "Stud partition",
                    "quantity": 20,
                    "unit": "m2",
                    "unit_cost": 60,
                    "unit_price": 90,
                }
            ],
            "unpriced": [],
            "confidence": 0.9,
        },
    )
    survey = _publish(
        rt,
        "survey.completed",
        {"survey_id": str(uuid.uuid4()), "site_id": str(uuid.uuid4()), "findings": ["Open plan"]},
        job_id,
        "Survey completed",
    )
    assert "estimator" in rt.tick()
    scope_run = _run("scope_writing", survey)
    _approve_next(scope_run)
    DBOS.retrieve_workflow(scope_run.id).get_result()
    approved = _packets("scope.approved")
    assert len(approved) == 1 and approved[0].payload["areas"] == ["Open plan"]

    assert "estimator" in rt.tick()
    price_run = _run("pricing", approved[0].id)
    _approve_next(price_run)
    assert DBOS.retrieve_workflow(price_run.id).get_result()["status"] == "done"
    ready = _packets("estimate.ready")
    assert len(ready) == 1 and ready[0].payload["currency"]
    assert _packets("escalation.raised") == []
    assert all(c["tier"] == "cloud" for c in cloud.calls)
