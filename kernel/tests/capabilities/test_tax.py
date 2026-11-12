"""Stored amounts are tax-exclusive; what a client owes (and what we owe) includes the tax.

The accounting system adds tax on top of the exclusive lines we post, so the "paid" check,
reminders and the cash forecast must use the tax-inclusive figure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from biz_cap_invoicing import config as inv_config
from biz_cap_invoicing.models import Invoice
from biz_cap_invoicing.tools import register as register_invoicing
from biz_cap_payables import config as pay_config
from biz_cap_payables.tools import register as register_payables
from biz_kernel.records.models import Job, Party, Person
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeAccounting
from sqlalchemy.orm import Session


class Recording(FakeAccounting):
    """Keeps the drafts it is asked to post."""

    def __init__(self) -> None:
        super().__init__()
        self.drafts: list[Any] = []

    async def create_invoice(self, inv: Any) -> Any:
        self.drafts.append(inv)
        return await super().create_invoice(inv)


@pytest.fixture
def stack() -> Any:
    manifest = SimpleNamespace(policy={"tax": {"rate_pct": 15}})
    inv_config.bind(manifest)
    pay_config.bind(manifest)
    reg, acct = ToolRegistry(), Recording()
    acct.tax_rate_pct = Decimal(15)
    register_invoicing(reg, {"accounting": acct})
    register_payables(reg, {"accounting": acct})
    yield reg, acct
    inv_config.bind(None)
    pay_config.bind(None)


def _call(reg: ToolRegistry, session: Session, tool: str, **args: Any) -> Any:
    return reg.get(tool).fn(session=session, **args)


def _invoice(reg: ToolRegistry, session: Session) -> dict[str, Any]:
    party = Party(kind="organisation", name="Client Co")
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name="Pat", email="pat@example.com"))
    job = Job(
        kind="project",
        stage="won",
        client_party_id=party.id,
        value_amount=Decimal("10000"),
        currency="NZD",
    )
    session.add(job)
    session.flush()
    _call(
        reg,
        session,
        "invoicing.stages_set",
        job_id=str(job.id),
        stages=[{"name": "Deposit", "pct": 30}],
    )
    out: dict[str, Any] = _call(
        reg,
        session,
        "invoicing.milestone_invoice",
        job_id=str(job.id),
        milestone_id=str(uuid.uuid4()),
        name="Deposit",
    )
    return out


def test_invoice_keeps_the_exclusive_amount_and_its_tax(session: Session, stack: Any) -> None:
    reg, acct = stack
    out = _invoice(reg, session)
    assert (out["amount"], out["tax"], out["gross"]) == ("3000.00", "450.00", "3450.00")
    assert out["total_minor"] == 300000  # packets and the approval carry the exclusive amount
    # Posted at the exclusive amount: the accounting system adds the tax itself.
    posted = _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=out["invoice_id"],
        amount="3000.00",
        idempotency_key="k1",
    )
    assert posted["status"] == "posted"
    (draft,) = acct.drafts
    assert sum(line.quantity * line.unit_amount for line in draft.lines) == Decimal("3000.00")


def test_what_is_owed_includes_tax(session: Session, stack: Any) -> None:
    reg, _ = stack
    out = _invoice(reg, session)
    _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=out["invoice_id"],
        amount="3000.00",
        idempotency_key="k1",
    )
    (item,) = _call(reg, session, "invoicing.receivables")["items"]
    assert item["amount"] == "3450.00"
    letter = _call(
        reg, session, "invoicing.reminder_draft", invoice_id=out["invoice_id"], reminder=1
    )
    assert "3450.00 NZD" in letter["body"]
    inv = session.get(Invoice, uuid.UUID(out["invoice_id"]))
    assert inv is not None
    inv.paid = Decimal("3000.00")  # the exclusive amount alone does not settle it
    assert not inv.settled and inv.owed == Decimal("450.00")
    inv.paid = Decimal("3450.00")
    assert inv.settled and inv.owed == Decimal("0.00")


def test_bill_tax_defaults_to_the_rate_and_can_be_stated(session: Session, stack: Any) -> None:
    reg, _ = stack
    supplier = Party(kind="organisation", name="Supplier Ltd")
    session.add(supplier)
    session.flush()
    args = {"supplier_party_id": str(supplier.id), "currency": "NZD"}
    a = _call(reg, session, "payables.bill_record", number="B-1", total="1000", **args)
    assert (a["amount"], a["tax"], a["gross"]) == ("1000.00", "150.00", "1150.00")
    # A supplier that charges no tax: the bill says so.
    b = _call(reg, session, "payables.bill_record", number="B-2", total="200", tax=0, **args)
    assert (b["amount"], b["tax"], b["gross"]) == ("200.00", "0.00", "200.00")


def test_what_we_owe_includes_tax(session: Session, stack: Any) -> None:
    from biz_cap_payables.models import Bill

    reg, _ = stack
    supplier = Party(kind="organisation", name="Supplier Ltd")
    session.add(supplier)
    session.flush()
    out = _call(
        reg,
        session,
        "payables.bill_record",
        supplier_party_id=str(supplier.id),
        number="B-1",
        total="1000",
        currency="NZD",
    )
    bill = session.get(Bill, uuid.UUID(out["bill_id"]))
    assert bill is not None
    bill.status = "posted"
    session.flush()
    (item,) = _call(reg, session, "payables.due")["items"]
    assert item["amount"] == "1150.00"


@pytest.mark.parametrize("bad", [-1, 101, "15", True, float("nan")])
def test_a_bad_tax_rate_is_refused(bad: Any) -> None:
    inv_config.bind(SimpleNamespace(policy={"tax": {"rate_pct": bad}}))
    pay_config.bind(SimpleNamespace(policy={"tax": {"rate_pct": bad}}))
    try:
        with pytest.raises(ValueError, match="tax.rate_pct"):
            inv_config.tax_rate()
        with pytest.raises(ValueError, match="tax.rate_pct"):
            pay_config.tax_rate()
    finally:
        inv_config.bind(None)
        pay_config.bind(None)


def test_no_tax_configured_changes_nothing(session: Session) -> None:
    inv_config.bind(None)
    reg = ToolRegistry()
    register_invoicing(reg, {"accounting": FakeAccounting()})
    out = _invoice(reg, session)
    assert (out["amount"], out["tax"], out["gross"]) == ("3000.00", "0.00", "3000.00")


def test_tax_is_read_back_from_accounting_when_posted(session: Session, stack: Any) -> None:
    """Our rate is only the estimate before posting; the accounting system's figure wins."""
    reg, acct = stack
    acct.tax_rate_pct = Decimal("12.5")
    out = _invoice(reg, session)
    assert out["tax"] == "450.00"  # estimated at our 15%
    posted = _call(
        reg,
        session,
        "invoicing.invoice_post",
        invoice_id=out["invoice_id"],
        amount="3000.00",
        idempotency_key="k1",
    )
    assert (posted["amount"], posted["tax"], posted["gross"]) == ("3000.00", "375.00", "3375.00")
    (item,) = _call(reg, session, "invoicing.receivables")["items"]
    assert item["amount"] == "3375.00"


def test_bill_tax_is_read_back_from_accounting_when_posted(session: Session, stack: Any) -> None:
    from biz_cap_payables.models import Bill

    reg, acct = stack
    acct.tax_rate_pct = Decimal("12.5")
    supplier = Party(kind="organisation", name="Supplier Ltd")
    session.add(supplier)
    session.flush()
    out = _call(
        reg,
        session,
        "payables.bill_record",
        supplier_party_id=str(supplier.id),
        number="B-1",
        total="1000",
        currency="NZD",
    )
    bill = session.get(Bill, uuid.UUID(out["bill_id"]))
    assert bill is not None
    bill.status = "matched"
    session.flush()
    posted = _call(
        reg,
        session,
        "payables.bill_post",
        bill_id=out["bill_id"],
        amount="1000",
        idempotency_key="b1",
    )
    assert (posted["tax"], posted["gross"]) == ("125.00", "1125.00")


def test_an_accounting_system_that_reports_no_tax_keeps_the_estimate(session: Session) -> None:
    class Silent(FakeAccounting):
        async def create_invoice(self, inv: Any) -> Any:
            doc = await super().create_invoice(inv)
            return doc.model_copy(update={"tax": None, "total": None})

    inv_config.bind(SimpleNamespace(policy={"tax": {"rate_pct": 15}}))
    try:
        reg = ToolRegistry()
        register_invoicing(reg, {"accounting": Silent()})
        out = _invoice(reg, session)
        posted = _call(
            reg,
            session,
            "invoicing.invoice_post",
            invoice_id=out["invoice_id"],
            amount="3000.00",
            idempotency_key="k1",
        )
        assert posted["tax"] == "450.00"
    finally:
        inv_config.bind(None)
