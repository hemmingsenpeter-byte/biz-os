"""Fake implementations of every port. Deterministic, offline, record what they were asked."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import jsonschema
from biz_kernel.contracts.ports import (
    BillDraft,
    CalendarEvent,
    ExternalRef,
    InboundMessage,
    InvoiceDraft,
    OutboundMessage,
    Party,
    Payment,
    SenderAuth,
    SendResult,
    Signer,
    SignEvent,
    Slot,
    StoredObject,
    TelephonyEvent,
    Transcript,
)


def _body(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        form = request.get("form")  # the shared webhook contract: {"form", "headers", "url"}
        return form if isinstance(form, dict) else request
    raise TypeError("fake webhooks take a dict")


class _Sender:
    """Idempotent delivery: same key returns the first result and delivers once."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self._results: dict[str, SendResult] = {}
        self.fail_next: str | None = None  # None | "failed" | "uncertain"

    def _deliver(self, key: str, item: Any) -> SendResult:
        if key in self._results:
            return self._results[key]
        if self.fail_next:
            status, self.fail_next = self.fail_next, None
            result = SendResult(status=status, error="scripted")  # type: ignore[arg-type]
            if status == "uncertain":
                self.sent.append(item)  # may have gone out
            return result
        self.sent.append(item)
        result = SendResult(
            status="sent", provider_id=f"fake-{len(self.sent)}-{uuid.uuid4().hex[:8]}"
        )
        self._results[key] = result
        return result


class FakeMail(_Sender):
    def __init__(self) -> None:
        super().__init__()
        self.inbox: list[InboundMessage] = []

    def inject_inbound(self, msg: InboundMessage, auth: SenderAuth | None = None) -> None:
        """Queue a message; `auth` sets the receiving server's verdict (default: unverified)."""
        self.inbox.append(msg if auth is None else msg.model_copy(update={"auth": auth}))

    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult:
        return self._deliver(idempotency_key, msg)

    async def fetch_new(self, since: datetime) -> list[InboundMessage]:
        return [m for m in self.inbox if m.received_at > since]


class FakeTelephony(_Sender):
    async def send_sms(self, to: str, body: str, idempotency_key: str) -> SendResult:
        return self._deliver(idempotency_key, (to, body))

    def parse_webhook(self, request: Any) -> TelephonyEvent:
        data = _body(request)
        return TelephonyEvent(
            kind=data.get("kind", "other"),
            from_number=data.get("from", ""),
            to_number=data.get("to", ""),
            body=data.get("body"),
            provider_id=data.get("id"),
        )


class FakeAccounting:
    def __init__(self) -> None:
        self.contacts: dict[uuid.UUID, ExternalRef] = {}
        self.invoices: dict[str, ExternalRef] = {}
        self.bills: dict[str, ExternalRef] = {}
        self.payments: list[Payment] = []
        self.balance: tuple[Decimal, str] = (Decimal(0), "NZD")

    async def upsert_contact(self, party: Party) -> ExternalRef:
        ref = self.contacts.setdefault(party.id, ExternalRef(system="fake", id=f"c-{party.id}"))
        return ref

    async def create_invoice(self, inv: InvoiceDraft) -> ExternalRef:
        key = inv.idempotency_key or inv.reference
        return self.invoices.setdefault(
            key, ExternalRef(system="fake", id=f"inv-{len(self.invoices) + 1}")
        )

    async def create_bill(self, bill: BillDraft) -> ExternalRef:
        key = bill.idempotency_key or bill.reference
        return self.bills.setdefault(
            key, ExternalRef(system="fake", id=f"bill-{len(self.bills) + 1}")
        )

    async def list_payments(self, since: datetime) -> list[Payment]:
        return [p for p in self.payments if p.paid_at > since]

    async def bank_balance(self) -> tuple[Decimal, str]:
        return self.balance


class FakeSign:
    def __init__(self) -> None:
        self.requests: list[tuple[uuid.UUID, list[Signer]]] = []

    async def request_signature(self, doc_id: uuid.UUID, signers: list[Signer]) -> ExternalRef:
        self.requests.append((doc_id, signers))
        return ExternalRef(system="fake", id=f"sig-{len(self.requests)}")

    def parse_webhook(self, request: Any) -> SignEvent:
        data = _body(request)
        return SignEvent(
            kind=data.get("kind", "other"),
            request_id=data.get("request_id", ""),
            signer_email=data.get("signer_email"),
            at=datetime.now(UTC),
        )


class FakeStorage:
    """In-memory content-addressed storage under fake://<sha256>."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes, name: str, content_type: str) -> StoredObject:
        sha = hashlib.sha256(data).hexdigest()
        uri = f"fake://{sha}"
        self.objects.setdefault(uri, data)
        return StoredObject(uri=uri, sha256=sha, size=len(data))

    def get(self, uri: str) -> bytes:
        return self.objects[uri]

    def exists(self, uri: str) -> bool:
        return uri in self.objects


def _sample(schema: dict[str, Any]) -> Any:
    """Smallest value that satisfies a simple JSON Schema."""
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    t = schema.get("type")
    if isinstance(t, list):
        t = t[0]
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _sample(props[k]) if k in props else "x" for k in schema.get("required", [])}
    if t == "array":
        return []
    if t in ("integer", "number"):
        return schema.get("minimum", 0)
    if t == "boolean":
        return False
    if t == "null":
        return None
    return "x"


class FakeModel:
    """Returns scripted dicts (queue, then default), else a minimal valid sample."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def script(self, *responses: dict[str, Any]) -> None:
        self.responses.extend(responses)

    async def structured(
        self,
        system: str,
        messages: list[Any],
        schema: dict[str, Any],
        tier: Literal["local", "cloud"],
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "messages": messages, "schema": schema, "tier": tier})
        if self.fail_with is not None:
            raise self.fail_with
        out = self.responses.pop(0) if self.responses else _sample(schema)
        jsonschema.validate(out, schema)
        return out


class FakeSpeech:
    def __init__(self, text: str = "fake transcript") -> None:
        self.text = text
        self.calls: list[str] = []

    async def transcribe(self, audio_uri: str) -> Transcript:
        self.calls.append(audio_uri)
        return Transcript(text=self.text, language="en", duration_s=1.0)


class FakeCalendar:
    """Working hours 09:00-17:00 UTC on weekdays, minus anything booked."""

    def __init__(self) -> None:
        self.events: dict[str, CalendarEvent] = {}

    async def free_slots(self, start: datetime, end: datetime, duration_minutes: int) -> list[Slot]:
        step = timedelta(minutes=duration_minutes)
        cur = start.replace(minute=0, second=0, microsecond=0)
        out: list[Slot] = []
        while cur + step <= end:
            busy = any(e.start < cur + step and cur < e.end for e in self.events.values())
            if cur.weekday() < 5 and 9 <= cur.hour < 17 and cur >= start and not busy:
                out.append(Slot(start=cur, end=cur + step))
            cur += step
        return out

    async def book(self, event: CalendarEvent, idempotency_key: str) -> ExternalRef:
        self.events.setdefault(idempotency_key, event)
        return ExternalRef(system="fake_calendar", id=f"evt-{idempotency_key}")


FAKES: dict[str, type] = {
    "fake_calendar": FakeCalendar,
    "fake_mail": FakeMail,
    "fake_telephony": FakeTelephony,
    "fake_accounting": FakeAccounting,
    "fake_sign": FakeSign,
    "fake_model": FakeModel,
    "fake_speech": FakeSpeech,
    "fake_storage": FakeStorage,
}
