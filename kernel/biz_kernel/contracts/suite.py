"""Reusable contract checks: run against any adapter (fake or real sandbox) of a port.

Each check raises AssertionError on a violation. Adapters may expose a `sent` list so the
suite can confirm that a repeated idempotency key produced exactly one delivery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import jsonschema

from biz_kernel.contracts.ports import (
    AccountingPort,
    BillDraft,
    CalendarEvent,
    CalendarPort,
    ExternalRef,
    InboundMessage,
    InvoiceDraft,
    LineItem,
    MailPort,
    ModelPort,
    OutboundMessage,
    Party,
    Payment,
    SendResult,
    Signer,
    SignEvent,
    SignPort,
    Slot,
    SpeechPort,
    StoragePort,
    StoredObject,
    TelephonyEvent,
    TelephonyPort,
    Transcript,
)

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _check_idempotent(first: SendResult, second: SendResult, port: Any, expected: int = 1) -> None:
    assert isinstance(first, SendResult) and isinstance(second, SendResult)
    assert first.status == "sent", first
    assert second.status == "sent", second
    assert first.provider_id and first.provider_id == second.provider_id, "same key, same id"
    sent = getattr(port, "sent", None)
    if sent is not None:
        assert len(sent) == expected, f"expected {expected} delivery, saw {len(sent)}"


async def check_mail_port(port: MailPort) -> None:
    key = f"k-{uuid.uuid4()}"
    msg = OutboundMessage(to=["a@example.com"], subject="Hello", body="Body")
    first = await port.send(msg, key)
    second = await port.send(msg, key)
    _check_idempotent(first, second, port)
    other = await port.send(msg, key + "-other")
    assert other.provider_id != first.provider_id, "new key must be a new delivery"
    fetched = await port.fetch_new(EPOCH)
    assert isinstance(fetched, list)
    assert all(isinstance(m, InboundMessage) for m in fetched)


async def check_telephony_port(port: TelephonyPort, sample_webhook: Any | None = None) -> None:
    key = f"k-{uuid.uuid4()}"
    first = await port.send_sms("+15550001", "hi", key)
    second = await port.send_sms("+15550001", "hi", key)
    _check_idempotent(first, second, port)
    if sample_webhook is not None:
        event = port.parse_webhook(sample_webhook)
        assert isinstance(event, TelephonyEvent)
        assert event.kind in ("missed_call", "inbound_sms", "other")


async def check_accounting_port(port: AccountingPort) -> None:
    party = Party(id=uuid.uuid4(), name="Contact", email="c@example.com")
    first = await port.upsert_contact(party)
    second = await port.upsert_contact(party)
    assert isinstance(first, ExternalRef) and first == second, "upsert is stable"
    lines = [LineItem(description="Item", unit_amount=Decimal("10.00"))]
    inv = InvoiceDraft(party=party, reference="R1", lines=lines, idempotency_key="inv-1")
    ref = await port.create_invoice(inv)
    assert isinstance(ref, ExternalRef) and ref.id
    bill = BillDraft(party=party, reference="B1", lines=lines, idempotency_key="bill-1")
    assert isinstance(await port.create_bill(bill), ExternalRef)
    payments = await port.list_payments(EPOCH)
    assert isinstance(payments, list) and all(isinstance(p, Payment) for p in payments)
    future = await port.list_payments(datetime.now(UTC) + timedelta(days=365))
    assert future == [], "nothing paid in the future"


async def check_sign_port(port: SignPort, sample_webhook: Any | None = None) -> None:
    signers = [Signer(name="A", email="a@example.com")]
    ref = await port.request_signature(uuid.uuid4(), signers)
    assert isinstance(ref, ExternalRef) and ref.id
    if sample_webhook is not None:
        assert isinstance(port.parse_webhook(sample_webhook), SignEvent)


async def check_model_port(port: ModelPort) -> None:
    schema = {
        "type": "object",
        "required": ["label", "score"],
        "properties": {"label": {"type": "string"}, "score": {"type": "number"}},
    }
    for tier in ("local", "cloud"):
        out = await port.structured("system", [{"role": "user", "content": "x"}], schema, tier)  # type: ignore[arg-type]
        assert isinstance(out, dict)
        jsonschema.validate(out, schema)


async def check_speech_port(port: SpeechPort) -> None:
    t = await port.transcribe("file:///audio.wav")
    assert isinstance(t, Transcript) and isinstance(t.text, str)


async def check_calendar_port(port: CalendarPort) -> None:
    start = datetime(2030, 1, 7, 0, 0, tzinfo=UTC)  # a Monday
    slots = await port.free_slots(start, start + timedelta(days=1), 60)
    assert slots and all(isinstance(s, Slot) for s in slots)
    first = slots[0]
    event = CalendarEvent(
        title="Meeting", start=first.start, end=first.end, attendees=["a@example.com"]
    )
    key = f"k-{uuid.uuid4()}"
    ref1 = await port.book(event, key)
    ref2 = await port.book(event, key)
    assert ref1 == ref2, "same key must not book twice"
    after = await port.free_slots(start, start + timedelta(days=1), 60)
    assert first not in after, "a booked slot must no longer be free"


def check_storage_port(port: StoragePort) -> None:
    data = f"contract {uuid.uuid4()}".encode()
    first = port.put(data, "doc.txt", "text/plain")
    second = port.put(data, "other-name.txt", "text/plain")
    assert isinstance(first, StoredObject) and first.uri, first
    assert first.size == len(data)
    assert len(first.sha256) == 64
    assert first == second, "same bytes must give the same object"
    assert port.exists(first.uri)
    assert port.get(first.uri) == data
    other = port.put(data + b"!", "doc.txt", "text/plain")
    assert other.uri != first.uri and other.sha256 != first.sha256
    assert not port.exists(first.uri + "-missing")
