"""Port protocols (outside systems) and the Pydantic types they exchange."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "AccountingPort",
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
    "SignEvent",
    "SignPort",
    "Signer",
    "SpeechPort",
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


class InboundMessage(BaseModel):
    provider_id: str
    sender: str
    to: list[str]
    subject: str
    body: str
    received_at: datetime
    thread_id: str | None = None


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


@runtime_checkable
class MailPort(Protocol):
    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult: ...
    async def fetch_new(self, since: datetime) -> list[InboundMessage]: ...


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
