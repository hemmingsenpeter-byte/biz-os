"""Port protocols (outside systems) and the Pydantic types they exchange."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "AccountingPort",
    "AuthVerdict",
    "BillDraft",
    "ExternalRef",
    "InboundMessage",
    "InvoiceDraft",
    "LineItem",
    "MailPort",
    "ModelPort",
    "OutboundMessage",
    "Party",
    "Payment",
    "SendResult",
    "SenderAuth",
    "SignEvent",
    "SignPort",
    "Signer",
    "SpeechPort",
    "StoragePort",
    "StoredObject",
    "TelephonyEvent",
    "TelephonyPort",
    "Transcript",
]


class Party(BaseModel):
    """A person or organisation as seen by an outside system."""

    id: UUID
    name: str
    email: str | None = None
    phone: str | None = None


class ExternalRef(BaseModel):
    """Identifier of an object in an outside system."""

    system: str
    id: str
    url: str | None = None


class LineItem(BaseModel):
    description: str
    quantity: Decimal = Decimal(1)
    unit_amount: Decimal
    tax_rate: Decimal = Decimal(0)


class InvoiceDraft(BaseModel):
    party: Party
    reference: str
    currency: str = "NZD"
    lines: list[LineItem]
    due: datetime | None = None
    idempotency_key: str | None = None


class BillDraft(BaseModel):
    party: Party
    reference: str
    currency: str = "NZD"
    lines: list[LineItem]
    due: datetime | None = None
    idempotency_key: str | None = None


class Payment(BaseModel):
    ref: ExternalRef
    amount: Decimal
    currency: str = "NZD"
    paid_at: datetime
    invoice_ref: ExternalRef | None = None


class OutboundMessage(BaseModel):
    to: list[str]
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    attachments: list[str] = Field(default_factory=list)


AuthVerdict = Literal["pass", "fail", "none"]


def _domain(value: str) -> str:
    return value.strip().rstrip(".").lower()


class SenderAuth(BaseModel):
    """The receiving mail server's verdict on an inbound message's sender (RFC 8601
    Authentication-Results). `fail` means checked and not passed (fail, softfail, neutral,
    temperror, ...); `none` means not checked or not known. An adapter fills this only from a
    header its own receiving server added, never from one the sender could have written, so
    the default is unverified."""

    dmarc: AuthVerdict = "none"
    spf: AuthVerdict = "none"
    dkim: AuthVerdict = "none"
    from_domain: str = ""  # the From domain DMARC was evaluated for (header.from)
    authserv_id: str = ""  # the server that produced the verdict

    def verifies(self, sender: str) -> bool:
        """True only on a DMARC pass for the domain of `sender` (the From address read)."""
        _, at, domain = sender.strip().rpartition("@")
        return (
            self.dmarc == "pass"
            and bool(at)
            and bool(_domain(domain))
            and _domain(domain) == _domain(self.from_domain)
        )


class InboundMessage(BaseModel):
    provider_id: str
    sender: str
    to: list[str]
    subject: str
    body: str
    received_at: datetime
    thread_id: str | None = None
    auth: SenderAuth = Field(default_factory=SenderAuth)  # default: unverified


class SendResult(BaseModel):
    """`uncertain` means the provider may have sent it: reconcile before any retry."""

    status: Literal["sent", "uncertain", "failed"]
    provider_id: str | None = None
    error: str | None = None


class TelephonyEvent(BaseModel):
    kind: Literal["missed_call", "inbound_sms", "other"]
    from_number: str
    to_number: str
    body: str | None = None
    provider_id: str | None = None
    at: datetime | None = None


class Signer(BaseModel):
    name: str
    email: str
    role: str = "signer"


class SignEvent(BaseModel):
    kind: Literal["viewed", "signed", "declined", "completed", "other"]
    request_id: str
    signer_email: str | None = None
    at: datetime | None = None


class StoredObject(BaseModel):
    uri: str
    sha256: str
    size: int


class Transcript(BaseModel):
    text: str
    language: str | None = None
    duration_s: float | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)


@runtime_checkable
class AccountingPort(Protocol):
    async def upsert_contact(self, party: Party) -> ExternalRef: ...
    async def create_invoice(self, inv: InvoiceDraft) -> ExternalRef: ...
    async def create_bill(self, bill: BillDraft) -> ExternalRef: ...
    async def list_payments(self, since: datetime) -> list[Payment]: ...
    async def bank_balance(self) -> tuple[Decimal, str]:
        """Total closing balance of the bank accounts, in the base currency: (amount, code)."""
        ...


@runtime_checkable
class MailPort(Protocol):
    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult: ...
    async def fetch_new(self, since: datetime) -> list[InboundMessage]: ...


class Slot(BaseModel):
    start: datetime
    end: datetime


class CalendarEvent(BaseModel):
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    body: str | None = None


@runtime_checkable
class CalendarPort(Protocol):
    """Free/busy and event booking. Booking is idempotent like sends."""

    async def free_slots(
        self, start: datetime, end: datetime, duration_minutes: int
    ) -> list[Slot]: ...
    async def book(self, event: CalendarEvent, idempotency_key: str) -> ExternalRef: ...


# Webhook contract shared by every port with parse_webhook: the capability passes
# {"form": <body fields>, "headers": <request headers>, "url": <URL the app received>}.


@runtime_checkable
class TelephonyPort(Protocol):
    async def send_sms(self, to: str, body: str, idempotency_key: str) -> SendResult: ...
    def parse_webhook(self, request: Any) -> TelephonyEvent:
        """Missed call or inbound SMS."""
        ...


@runtime_checkable
class SignPort(Protocol):
    async def request_signature(self, doc_id: UUID, signers: list[Signer]) -> ExternalRef: ...
    def parse_webhook(self, request: Any) -> SignEvent: ...


@runtime_checkable
class ModelPort(Protocol):
    async def structured(
        self,
        system: str,
        messages: list[Any],
        schema: dict[str, Any],
        tier: Literal["local", "cloud"],
    ) -> dict[str, Any]: ...


@runtime_checkable
class SpeechPort(Protocol):
    async def transcribe(self, audio_uri: str) -> Transcript: ...


@runtime_checkable
class StoragePort(Protocol):
    """Content-addressed blob storage: putting the same bytes twice yields the same object."""

    def put(self, data: bytes, name: str, content_type: str) -> StoredObject: ...
    def get(self, uri: str) -> bytes: ...
    def exists(self, uri: str) -> bool: ...
