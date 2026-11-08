"""M6 (first slice): milestone reached -> invoice drafted -> owner approves -> posted as a
draft to accounting -> invoice.issued. Runs against apps/demo and apps/hive with every port
faked."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from biz_kernel import db
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from biz_kernel.records.models import Job, Party, Person, Task
from biz_kernel.runtime import build_runtime
from biz_kernel.workflows.gate import decide
from biz_kernel.workflows.models import SopRun
from biz_port_fakes import (
    FakeAccounting,
    FakeCalendar,
    FakeMail,
    FakeModel,
    FakeSign,
    FakeSpeech,
    FakeStorage,
    FakeTelephony,
)
from dbos import DBOS
from sqlalchemy import create_engine, make_url, select

ROOT = Path(__file__).parents[2]


def _wait(pred, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


@pytest.fixture(params=["demo", "hive"])
def rt(fresh_db: str, monkeypatch, request):  # type: ignore[no-untyped-def]
    import biz_cap_booking.models  # noqa: F401
    import biz_cap_comms.models  # noqa: F401
    import biz_cap_documents.models  # noqa: F401
    import biz_cap_intake.models  # noqa: F401
    import biz_cap_invoicing.models  # noqa: F401
    import biz_cap_payables.models  # noqa: F401
    import biz_cap_quoting.models  # noqa: F401

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    url = make_url(fresh_db)
    sys_url = url.set(database=f"{url.database}_sys").render_as_string(hide_password=False)

    def no_real_ports(name: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"port {name} was not overridden with a fake")

    monkeypatch.setattr("biz_kernel.runtime.resolve_port", no_real_ports)
    ports = {
        "mail": FakeMail(),
        "telephony": FakeTelephony(),
        "calendar": FakeCalendar(),
        "model_local": FakeModel(),
        "model_cloud": FakeModel(),
        "storage": FakeStorage(),
        "sign": FakeSign(),
        "speech": FakeSpeech(),
        "accounting": FakeAccounting(),
    }
    # Every tick also runs the weekly cash review; keep it above demo's floor by default.
    ports["accounting"].balance = (Decimal("50000"), "NZD")
    runtime = build_runtime(
        ROOT / "apps" / request.param, ROOT, ports=ports, approval_poll_seconds=1
    )
    DBOS(config={"name": "biz-e2e", "system_database_url": sys_url, "application_version": "test"})
    DBOS.launch()
    yield runtime
    DBOS.destroy(destroy_registry=False)
    eng.dispose()


def _seed(rt: Any) -> uuid.UUID:
    with db.session_scope() as s:
        party = Party(kind="organisation", name="Client Co")
        s.add(party)
        s.flush()
        s.add(Person(party_id=party.id, full_name="Pat", email="pat@example.com"))
        job = Job(
            kind="project",
            stage="won",
            client_party_id=party.id,
            value_amount=Decimal("20000"),
            currency="NZD",
        )
        s.add(job)
        s.flush()
        rt.registry.get("invoicing.stages_set").fn(
            session=s,
            job_id=str(job.id),
            stages=[{"name": "Deposit", "pct": 25}, {"name": "Completion", "pct": 75}],
        )
        return job.id


def _milestone(rt: Any, job_id: uuid.UUID, name: str) -> uuid.UUID:
    with db.session_scope() as s:
        return rt.bus.publish(
            s,
            type="milestone.reached",
            from_part="test",
            summary=f"Milestone reached: {name}",
            payload={"milestone_id": str(uuid.uuid4()), "job_id": str(job_id), "name": name},
            job_id=job_id,
        ).id


def _run(packet_id: uuid.UUID) -> SopRun:
    def find() -> SopRun | None:
        with db.session_scope() as s:
            run = s.get(SopRun, f"bookkeeper:milestone_invoicing:{packet_id}")
            if run is not None:
                s.expunge(run)
            return run

    return _wait(find)


def _packets(type_: str) -> list[Packet]:
    with db.session_scope() as s:
        rows = list(s.scalars(select(Packet).where(Packet.type == type_)))
        s.expunge_all()
        return rows


def _waiting_op(run: SopRun) -> StagedOperation:
    def waiting():  # type: ignore[no-untyped-def]
        with db.session_scope() as s:
            r = s.get(SopRun, run.id)
            if r is None or r.status != "waiting" or not r.waiting_op_id:
                return None
            op = s.get(StagedOperation, uuid.UUID(r.waiting_op_id))
            s.expunge(op)
            return op

    return _wait(waiting)


def test_milestone_is_invoiced_after_approval(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    job_id = _seed(rt)
    packet_id = _milestone(rt, job_id, "Deposit")
    assert "bookkeeper" in rt.tick()
    run = _run(packet_id)
    op = _waiting_op(run)
    assert op.tool == "invoicing.invoice_post" and op.inputs["amount"] == "5000.00"
    assert acct.invoices == {}, "nothing is posted before the owner approves"
    decide(op.id, op.approver, approve=True, version=op.version)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "done"
    assert len(acct.invoices) == 1
    issued = _packets("invoice.issued")
    assert len(issued) == 1 and issued[0].payload["total_minor"] == 500000
    assert issued[0].payload["currency"] == "NZD"
    with db.session_scope() as s:
        assert s.get(Task, run.task_id).status == "done"


def test_unknown_stage_escalates_and_posts_nothing(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    job_id = _seed(rt)
    packet_id = _milestone(rt, job_id, "Handover")
    assert "bookkeeper" in rt.tick()
    run = _run(packet_id)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "escalated"
    assert acct.invoices == {} and _packets("invoice.issued") == []
    esc = _packets("escalation.raised")
    assert len(esc) == 1 and esc[0].payload["raised_by"] == "bookkeeper"
    with db.session_scope() as s:
        assert s.get(Task, run.task_id).status == "open"


def test_payment_is_reconciled_to_the_invoice_once(rt: Any) -> None:
    from biz_cap_invoicing.models import Invoice
    from biz_kernel.contracts.ports import Payment

    acct: FakeAccounting = rt.ports["accounting"]
    job_id = _seed(rt)
    packet_id = _milestone(rt, job_id, "Deposit")
    rt.tick()
    run = _run(packet_id)
    op = _waiting_op(run)
    decide(op.id, op.approver, approve=True, version=op.version)
    DBOS.retrieve_workflow(run.id).get_result()
    posted = next(iter(acct.invoices.values()))
    acct.payments.append(
        Payment(
            ref=posted.model_copy(update={"id": "pay-1"}),
            amount=Decimal("5000.00"),
            currency="NZD",
            paid_at=db.utcnow(),
            invoice_ref=posted,
        )
    )
    assert rt.poll() == 1
    assert rt.poll() == 0  # the same payment is never counted twice
    received = _packets("payment.received")
    assert len(received) == 1 and received[0].payload["amount_minor"] == 500000
    with db.session_scope() as s:
        inv = s.scalars(select(Invoice)).one()
        assert received[0].payload["invoice_id"] == str(inv.id)
        assert inv.status == "paid" and inv.paid == Decimal("5000.00")


def _bill_received(rt: Any, total: str) -> uuid.UUID:
    with db.session_scope() as s:
        supplier = Party(kind="organisation", name="Supplier Ltd")
        s.add(supplier)
        s.flush()
        sid = str(supplier.id)
        reg = rt.registry
        reg.get("payables.po_record").fn(
            session=s,
            po_id=str(uuid.uuid4()),
            supplier_party_id=sid,
            number="PO-7",
            total="1200",
            currency="NZD",
        )
        bill = reg.get("payables.bill_record").fn(
            session=s,
            supplier_party_id=sid,
            number="S-1",
            total=total,
            currency="NZD",
            po_number="PO-7",
        )
        return rt.bus.publish(
            s,
            type="bill.received",
            from_part="test",
            summary="Bill S-1 from Supplier Ltd",
            payload={
                "bill_id": bill["bill_id"],
                "supplier_party_id": sid,
                "total_minor": bill["total_minor"],
                "currency": "NZD",
            },
        ).id


def _bill_run(packet_id: uuid.UUID) -> SopRun:
    def find() -> SopRun | None:
        with db.session_scope() as s:
            run = s.get(SopRun, f"bookkeeper:bill_matching:{packet_id}")
            if run is not None:
                s.expunge(run)
            return run

    return _wait(find)


def test_matched_bill_is_posted_after_approval(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    packet_id = _bill_received(rt, "1200.00")
    assert "bookkeeper" in rt.tick()
    run = _bill_run(packet_id)
    op = _waiting_op(run)
    assert op.tool == "payables.bill_post" and op.inputs["amount"] == "1200.00"
    assert acct.bills == {}
    decide(op.id, op.approver, approve=True, version=op.version)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "done"
    assert len(acct.bills) == 1 and len(_packets("bill.matched")) == 1


def test_bill_over_its_po_escalates(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    packet_id = _bill_received(rt, "1500.00")
    assert "bookkeeper" in rt.tick()
    run = _bill_run(packet_id)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "escalated"
    assert acct.bills == {} and _packets("bill.matched") == []
    esc = _packets("escalation.raised")
    assert len(esc) == 1 and "bill_mismatch" in esc[0].payload["reason"]


def test_overdue_invoice_gets_one_approved_reminder_per_step(rt: Any) -> None:
    from datetime import timedelta

    from biz_cap_invoicing.models import Invoice

    mail: FakeMail = rt.ports["mail"]
    job_id = _seed(rt)
    packet_id = _milestone(rt, job_id, "Deposit")
    rt.tick()
    run = _run(packet_id)
    op = _waiting_op(run)
    decide(op.id, op.approver, approve=True, version=op.version)
    DBOS.retrieve_workflow(run.id).get_result()
    assert rt.poll() == 0  # not due yet
    with db.session_scope() as s:
        s.scalars(select(Invoice)).one().due = db.utcnow() - timedelta(days=8)
    assert "bookkeeper" in rt.tick()  # the poll inside publishes reminder 1
    overdue = _packets("invoice.overdue")
    assert len(overdue) == 1 and overdue[0].payload["reminder"] == 1

    def reminder_run() -> SopRun | None:
        with db.session_scope() as s:
            r = s.get(SopRun, f"bookkeeper:debtor_reminder:{overdue[0].id}")
            if r is not None:
                s.expunge(r)
            return r

    run = _wait(reminder_run)
    op = _waiting_op(run)
    assert op.tool == "comms.send" and op.inputs["to"] == "pat@example.com"
    assert "overdue" in op.inputs["subject"] and mail.sent == []
    decide(op.id, op.approver, approve=True, version=op.version)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "done"
    assert len(mail.sent) == 1
    assert rt.poll() == 0  # step 1 is done; step 2 waits until 14 days


def _sop_run(sop: str, packet_id: uuid.UUID) -> SopRun:
    def find() -> SopRun | None:
        with db.session_scope() as s:
            run = s.get(SopRun, f"bookkeeper:{sop}:{packet_id}")
            if run is not None:
                s.expunge(run)
            return run

    return _wait(find)


def test_issued_po_is_kept_and_its_bill_matches(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    with db.session_scope() as s:
        supplier = Party(kind="organisation", name="Supplier Ltd")
        s.add(supplier)
        s.flush()
        sid = str(supplier.id)
        po_id = rt.bus.publish(
            s,
            type="po.issued",
            from_part="test",
            summary="PO-12 issued to Supplier Ltd",
            payload={
                "po_id": str(uuid.uuid4()),
                "number": "PO-12",
                "supplier_party_id": sid,
                "total_minor": 120000,
                "currency": "NZD",
                "job_id": None,
            },
        ).id
    assert "bookkeeper" in rt.tick()
    run = _sop_run("po_recording", po_id)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "done"
    assert [p.payload["number"] for p in _packets("po.recorded")] == ["PO-12"]
    with db.session_scope() as s:
        bill = rt.registry.get("payables.bill_record").fn(
            session=s, supplier_party_id=sid, number="S-9", total="1200", currency="NZD"
        )
        matched = rt.registry.get("payables.bill_match").fn(session=s, bill_id=bill["bill_id"])
    assert matched["status"] == "matched" and acct.bills == {}


def _forecast_run(rt: Any) -> SopRun:
    due = _wait(lambda: _packets("cash.review_due"))
    assert len(due) == 1, "one review per local week"
    return _sop_run("cash_review", due[0].id)


def test_weekly_forecast_opens_at_the_bank_balance(rt: Any) -> None:
    acct: FakeAccounting = rt.ports["accounting"]
    acct.balance = (Decimal("8000"), "NZD")
    assert "bookkeeper" in rt.tick()
    assert rt.poll() == 0  # the same week is never asked twice
    run = _forecast_run(rt)
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "done"
    ready = _packets("cash.forecast_ready")
    assert len(ready) == 1 and ready[0].payload["opening_balance"] == "8000.00"
    assert ready[0].payload["lowest_closing"] == "8000.00"
    with db.session_scope() as s:
        assert s.get(Task, run.task_id).status == "done"


def test_forecast_below_the_floor_escalates(rt: Any, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_cash import config

    monkeypatch.setattr(
        config, "setting", lambda name, default: "1000" if name == "floor" else default
    )
    rt.ports["accounting"].balance = (Decimal("200"), "NZD")
    rt.tick()
    run = _forecast_run(rt)
    assert _packets("cash.review_due")[0].payload["floor"] == "1000"
    assert DBOS.retrieve_workflow(run.id).get_result()["status"] == "escalated"
    esc = _packets("escalation.raised")
    assert len(esc) == 1 and "cash_below_floor" in esc[0].payload["reason"]
    assert _packets("cash.forecast_ready") == []


def test_reminder_wording_comes_from_the_composition(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_invoicing import config
    from biz_cap_invoicing.tools import reminder_template

    def use(template: Any) -> None:
        monkeypatch.setattr(
            config, "setting", lambda n, d: template if n == "reminder_template" else d
        )

    use({"subject": "Kia ora: {reference} ({amount} {currency})"})
    out = reminder_template()
    assert out["subject"] == "Kia ora: {reference} ({amount} {currency})"
    assert "{due}" in out["body"], "an unset part keeps the default wording"
    for bad in (
        {"subject": "{customer_name}"},
        {"body": "{reference.__class__}"},
        {"body": "{amount:>99}"},
        {"body": "unclosed {reference"},
        {"subject": ""},
        {"footer": "x"},
    ):
        use(bad)
        with pytest.raises(ValueError):
            reminder_template()
