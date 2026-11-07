"""Invoicing: payment stages, milestone invoices, and posting exactly once at the stated amount."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from biz_cap_invoicing.models import Invoice
from biz_cap_invoicing.tools import register
from biz_kernel.records.models import Job, Party, Person
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeAccounting
from sqlalchemy.orm import Session


@pytest.fixture
def stack() -> Any:
    reg = ToolRegistry()
    acct = FakeAccounting()
    register(reg, {"accounting": acct})
    return reg, acct


def _call(reg: ToolRegistry, session: Session, tool: str, **args: Any) -> Any:
    return reg.get(tool).fn(session=session, **args)


def _job(session: Session, value: str | None = "10000", currency: str | None = "NZD") -> str:
    party = Party(kind="organisation", name="Client Co")
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name="Pat", email="pat@example.com"))
    job = Job(
        kind="project",
        stage="won",
        client_party_id=party.id,
        value_amount=Decimal(value) if value else None,
        currency=currency,
    )
    session.add(job)
    session.flush()
    return str(job.id)


def _stages(reg: ToolRegistry, session: Session, job: str) -> None:
    _call(
        reg,
        session,
        "invoicing.stages_set",
        job_id=job,
        stages=[{"name": "Deposit", "pct": 30}, {"name": "Final", "pct": 70}],
    )


def test_milestone_invoice_from_stage_share(session: Session, stack: Any) -> None:
    reg, _ = stack
    job = _job(session)
    _stages(reg, session, job)
    mid = str(uuid.uuid4())
    out = _call(
        reg, session, "invoicing.milestone_invoice", job_id=job, milestone_id=mid, name="Deposit"
    )
    assert out["amount"] == "3000.00" and out["total_minor"] == 300000
    assert out["currency"] == "NZD" and out["signals"] == []
    again = _call(
        reg, session, "invoicing.milestone_invoice", job_id=job, milestone_id=mid, name="Deposit"
    )
    assert again["invoice_id"] == out["invoice_id"] and again["replayed"]


@pytest.mark.parametrize(
    ("value", "currency", "stage", "why"),
    [
        ("10000", "NZD", "Nope", "no payment stage"),
        (None, "NZD", "Deposit", "job value"),
        ("10000", None, "Deposit", "no currency"),
    ],
)
def test_not_ready_signals_and_drafts_nothing(
    session: Session, stack: Any, value: Any, currency: Any, stage: str, why: str
) -> None:
    reg, _ = stack
    job = _job(session, value, currency)
    _stages(reg, session, job)
    out = _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=job,
        milestone_id=str(uuid.uuid4()),
        name=stage,
    )
    assert out["invoice_id"] is None and out["signals"] == ["invoice_not_ready"]
    assert any(why in p for p in out["problems"])
    assert session.query(Invoice).count() == 0


def test_never_invoices_past_the_job_value(session: Session, stack: Any) -> None:
    reg, _ = stack
    job = _job(session, "1000")
    _call(
        reg,
        session,
        "invoicing.stages_set",
        job_id=job,
        stages=[{"name": "A", "amount": 800}, {"name": "B", "amount": 300}],
    )
    _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=job,
        milestone_id=str(uuid.uuid4()),
        name="A",
    )
    out = _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=job,
        milestone_id=str(uuid.uuid4()),
        name="B",
    )
    assert out["signals"] == ["invoice_not_ready"] and "past its value" in out["problems"][0]


@pytest.mark.parametrize(
    "stages",
    [
        [{"name": "A", "pct": 60}, {"name": "B", "pct": 50}],  # over 100%
        [{"name": "A", "pct": -1}],
        [{"name": "A", "amount": "NaN"}],
        [{"name": "A", "amount": True}],
        [{"name": "A", "pct": 10, "amount": 10}],
        [{"name": "A"}],
        [{"name": "A", "pct": 10}, {"name": "A", "pct": 10}],
    ],
)
def test_bad_stages_are_refused(session: Session, stack: Any, stages: list[dict[str, Any]]) -> None:
    reg, _ = stack
    with pytest.raises(ValueError):
        _call(reg, session, "invoicing.stages_set", job_id=_job(session), stages=stages)


def test_stages_are_frozen_once_invoiced(session: Session, stack: Any) -> None:
    reg, _ = stack
    job = _job(session)
    _stages(reg, session, job)
    _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=job,
        milestone_id=str(uuid.uuid4()),
        name="Deposit",
    )
    with pytest.raises(ValueError, match="already has invoices"):
        _stages(reg, session, job)


def test_post_once_at_the_stated_amount(session: Session, stack: Any) -> None:
    reg, acct = stack
    job = _job(session)
    _stages(reg, session, job)
    inv = _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=job,
        milestone_id=str(uuid.uuid4()),
        name="Deposit",
    )
    with pytest.raises(ValueError, match="does not match"):
        _call(
            reg,
            session,
            "invoicing.invoice_post",
            invoice_id=inv["invoice_id"],
            amount="2999.99",
            idempotency_key="k",
        )
    assert acct.invoices == {}
    out = _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=inv["invoice_id"],
        amount="3000",
        idempotency_key="k",
    )
    assert out["status"] == "posted" and out["external_id"] == "inv-1"
    again = _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=inv["invoice_id"],
        amount="3000",
        idempotency_key="k",
    )
    assert again["replayed"] and len(acct.invoices) == 1
    with pytest.raises(ValueError, match="already posted"):
        _call(
            reg,
            session,
            "invoicing.invoice_post",
            invoice_id=inv["invoice_id"],
            amount="3000",
            idempotency_key="k2",
        )
    assert len(acct.contacts) == 1


def test_receivables_list_posted_unpaid_with_due_dates(session: Session, stack: Any) -> None:
    reg, _ = stack
    job = _job(session)
    _stages(reg, session, job)
    mid = str(uuid.uuid4())
    inv = _call(
        reg, session, "invoicing.milestone_invoice", job_id=job, milestone_id=mid, name="Deposit"
    )
    assert inv["due"] is not None
    assert _call(reg, session, "invoicing.receivables")["items"] == []  # drafts are not owed yet
    _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=inv["invoice_id"],
        amount="3000",
        idempotency_key="k",
    )
    items = _call(reg, session, "invoicing.receivables")["items"]
    assert [(i["invoice_id"], i["amount"]) for i in items] == [(inv["invoice_id"], "3000.00")]
