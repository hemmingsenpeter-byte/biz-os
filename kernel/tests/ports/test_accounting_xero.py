"""Xero adapter: drafts only, exactly once per key, reconciled after an uncertain create."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from biz_kernel.contracts import suite
from biz_kernel.contracts.ports import InvoiceDraft, LineItem, Party
from biz_port_accounting_xero import MemoryLedger, XeroAccounting, XeroFailed, XeroUncertain
from biz_port_accounting_xero.testing import FakeXero


def _xero(fake: FakeXero, ledger: MemoryLedger | None = None) -> XeroAccounting:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return XeroAccounting("id", "secret", tenant_id="t1", http=http, ledger=ledger)


def _draft(key: str = "k1", reference: str = "INV-1") -> InvoiceDraft:
    party = Party(id=uuid.uuid4(), name="Client Co", email="c@example.com")
    lines = [LineItem(description="Deposit", unit_amount=Decimal("5000.00"))]
    return InvoiceDraft(party=party, reference=reference, lines=lines, idempotency_key=key)


def test_passes_the_contract_suite() -> None:
    asyncio.run(suite.check_accounting_port(_xero(FakeXero())))


def test_invoice_is_a_draft_sent_once_per_key() -> None:
    fake = FakeXero()
    x = _xero(fake)
    draft = _draft()
    first = asyncio.run(x.create_invoice(draft))
    second = asyncio.run(x.create_invoice(draft))
    assert first == second and len(fake.invoices) == 1
    inv = fake.invoices[0]
    assert inv["Status"] == "DRAFT" and inv["Type"] == "ACCREC" and inv["Reference"] == "INV-1"
    assert inv["LineItems"][0]["UnitAmount"] == "5000.00"
    posts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/Invoices")]
    assert len(posts) == 1 and posts[0].headers["Xero-tenant-id"] == "t1"
    assert posts[0].headers["Idempotency-Key"]


def test_contact_is_found_by_party_id_not_name() -> None:
    fake = FakeXero()
    x = _xero(fake)
    party = Party(id=uuid.uuid4(), name="Client Co")
    a = asyncio.run(x.upsert_contact(party))
    b = asyncio.run(x.upsert_contact(party.model_copy(update={"name": "Renamed Co"})))
    assert a == b and len(fake.contacts) == 1


def test_uncertain_create_is_reconciled_not_resent() -> None:
    fake = FakeXero()
    ledger = MemoryLedger()
    x = _xero(fake, ledger)
    fake.fail_next_post = httpx.ReadTimeout("lost the response")
    with pytest.raises(XeroUncertain):
        asyncio.run(x.create_invoice(_draft()))
    assert len(fake.invoices) == 1  # it did reach Xero
    with pytest.raises(XeroUncertain, match="in flight"):
        asyncio.run(x.create_invoice(_draft()))  # too soon: may still be running
    ledger._data[next(iter(ledger._data))]["at"] -= timedelta(minutes=5)
    fake.by_key.clear()  # Xero's idempotency window has passed: only reconcile can save us
    ref = asyncio.run(x.create_invoice(_draft()))
    assert len(fake.invoices) == 1 and ref.id == fake.invoices[0]["InvoiceID"]


def test_validation_error_releases_the_key() -> None:
    fake = FakeXero()
    x = _xero(fake)
    fake.reject_next_post = "Account code '200' is not a valid code"
    with pytest.raises(XeroFailed, match="not a valid code"):
        asyncio.run(x.create_invoice(_draft()))
    assert fake.invoices == []
    assert asyncio.run(x.create_invoice(_draft())).id  # a fixed retry goes through


def test_payments_parse_xero_dates_and_skip_unauthorised() -> None:
    fake = FakeXero()
    x = _xero(fake)
    paid = datetime(2026, 11, 1, tzinfo=UTC)
    stamp = f"/Date({int(paid.timestamp() * 1000)}+0000)/"
    fake.payments = [
        {
            "PaymentID": "p1",
            "Amount": 5000.0,
            "Date": stamp,
            "Status": "AUTHORISED",
            "Invoice": {"InvoiceID": "i1", "CurrencyCode": "NZD"},
        },
        {"PaymentID": "p2", "Amount": 1.0, "Date": stamp, "Status": "DELETED", "Invoice": {}},
    ]
    out = asyncio.run(x.list_payments(paid - timedelta(days=1)))
    assert [p.ref.id for p in out] == ["p1"]
    assert out[0].paid_at == paid and out[0].amount == Decimal("5000.0")
    assert out[0].invoice_ref is not None and out[0].invoice_ref.id == "i1"
    get = [r for r in fake.requests if r.url.path.endswith("/Payments")][0]
    assert "If-Modified-Since" in get.headers


def test_bank_balance_sums_account_rows_not_the_total() -> None:
    fake = FakeXero()
    fake.bank_rows = [("Cheque", "1000.50"), ("Savings", "2500.00")]
    assert asyncio.run(_xero(fake).bank_balance()) == (Decimal("3500.50"), "NZD")
