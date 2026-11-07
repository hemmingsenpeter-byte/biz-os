"""M5: survey -> scope -> estimate -> approval, and the margin-below-policy escalation.

Runs against both apps/demo and apps/hive with every port faked."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
import yaml
from biz_kernel import db
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Job, Task
from biz_kernel.runtime import build_runtime
from biz_kernel.workflows.gate import decide
from biz_kernel.workflows.models import SopRun
from biz_port_fakes import FakeModel, FakeSign, FakeSpeech, FakeStorage
from dbos import DBOS
from sqlalchemy import create_engine, make_url, select

ROOT = Path(__file__).parents[2]


# Obviously fake test prices. Demo uses generic codes; hive uses the fit-out categories.
CATEGORY = {"demo": "labour", "hive": "partitions"}


def _rows(app: str) -> list[dict]:  # type: ignore[type-arg]
    return [
        {
            "code": "PT-01",
            "category": CATEGORY[app],
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
                    "category": "CAT",
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


@pytest.fixture(params=["demo", "hive"])
def app_name(request) -> str:  # type: ignore[no-untyped-def]
    return str(request.param)


@pytest.fixture
def rt(fresh_db: str, monkeypatch, app_name: str):  # type: ignore[no-untyped-def]
    APP = ROOT / "apps" / app_name
    eng = create_engine(fresh_db)
    import_all_models()
    import biz_cap_booking.models  # noqa: F401
    import biz_cap_comms.models  # noqa: F401
    import biz_cap_documents.models  # noqa: F401
    import biz_cap_intake.models  # noqa: F401
    import biz_cap_quoting.models  # noqa: F401

    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)

    def no_real_ports(name: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"port {name} was not overridden with a fake")

    monkeypatch.setattr("biz_kernel.runtime.resolve_port", no_real_ports)
    # PDF bytes are not under test here (WeasyPrint needs Pango); the stored document is.
    monkeypatch.setattr(
        "biz_cap_documents.render.html_to_pdf", lambda source: b"%PDF-1.4 fake " + source.encode()
    )
    manifest_ports = yaml.safe_load((APP / "composition.yaml").read_text())["ports"]
    fakes = {
        "sign": FakeSign(),
        "storage": FakeStorage(),
        "speech": FakeSpeech(),
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
    from biz_port_fakes import FakeAccounting, FakeCalendar, FakeMail, FakeTelephony

    return {
        "mail": FakeMail,
        "telephony": FakeTelephony,
        "calendar": FakeCalendar,
        "accounting": FakeAccounting,
    }[name]()


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


def _seed(rt, app: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    with db.session_scope() as s:
        job = Job(kind="fitout", stage="open")
        s.add(job)
        s.flush()
        job_id = job.id
    tool = rt.registry.get("quoting.price_book_import")
    with db.session_scope() as s:
        tool.fn(session=s, rows=_rows(app), source="test")
    return job_id


def _scope(app: str) -> dict:  # type: ignore[type-arg]
    out = json.loads(json.dumps(SCOPE))
    out["areas"][0]["items"][0]["category"] = CATEGORY[app]
    return out


def _lines(unit_price: int) -> dict:  # type: ignore[type-arg]
    return {
        "lines": [
            {
                "code": "PT-01",
                "description": "Stud partition",
                "quantity": 20,
                "unit": "m2",
                "unit_cost": 60,
                "unit_price": unit_price,
            }
        ],
        "unpriced": [],
        "confidence": 0.9,
    }


def _to_scope_approved(rt, job_id: uuid.UUID, app: str) -> Packet:  # type: ignore[no-untyped-def]
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
    assert approved[0].payload["scope_document_id"] != approved[0].payload["scope_id"]
    return approved[0]


def test_survey_to_scope_to_estimate(rt, app_name: str) -> None:  # type: ignore[no-untyped-def]
    job_id = _seed(rt, app_name)
    cloud: FakeModel = rt.ports["model_cloud"]
    cloud.script(_scope(app_name), _lines(90))
    approved = _to_scope_approved(rt, job_id, app_name)

    assert "estimator" in rt.tick()
    price_run = _run("pricing", approved.id)
    _approve_next(price_run)
    assert DBOS.retrieve_workflow(price_run.id).get_result()["status"] == "done"
    ready = _packets("estimate.ready")
    assert len(ready) == 1 and ready[0].payload["currency"]
    assert ready[0].payload["document_id"]
    assert ready[0].payload["total_minor"] == 20 * 90 * 100
    assert _packets("escalation.raised") == []
    assert all(c["tier"] == "cloud" for c in cloud.calls)


def test_below_margin_escalates(rt, app_name: str) -> None:  # type: ignore[no-untyped-def]
    job_id = _seed(rt, app_name)
    cloud: FakeModel = rt.ports["model_cloud"]
    cloud.script(_scope(app_name), _lines(62))
    approved = _to_scope_approved(rt, job_id, app_name)

    assert "estimator" in rt.tick()
    price_run = _run("pricing", approved.id)
    assert DBOS.retrieve_workflow(price_run.id).get_result()["status"] == "escalated"
    assert _packets("estimate.ready") == []
    esc = _packets("escalation.raised")
    assert len(esc) == 1 and esc[0].payload["raised_by"] == "estimator"
    with db.session_scope() as s:  # nothing claims the pricing was done
        assert s.get(Task, price_run.task_id).status == "open"


def test_low_confidence_pricing_escalates(rt, app_name: str) -> None:  # type: ignore[no-untyped-def]
    # pricing has its own margin escalation; the role's confidence_below still applies.
    job_id = _seed(rt, app_name)
    cloud: FakeModel = rt.ports["model_cloud"]
    cloud.script(_scope(app_name), {**_lines(90), "confidence": 0.3})
    approved = _to_scope_approved(rt, job_id, app_name)

    assert "estimator" in rt.tick()
    price_run = _run("pricing", approved.id)
    assert DBOS.retrieve_workflow(price_run.id).get_result()["status"] == "escalated"
    assert _packets("estimate.ready") == []
    esc = _packets("escalation.raised")
    assert len(esc) == 1 and "confidence" in esc[0].payload["reason"]
    with db.session_scope() as s:
        assert s.get(Task, price_run.task_id).status == "open"
