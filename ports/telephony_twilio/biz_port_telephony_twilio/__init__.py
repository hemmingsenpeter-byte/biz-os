"""Twilio adapter for TelephonyPort: SMS sending and verified webhook parsing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import requests
from biz_kernel.contracts.ports import SendResult, TelephonyEvent
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

__all__ = ["MemoryLedger", "SendLedger", "TwilioTelephony", "WebhookRejected", "create"]

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}


class WebhookRejected(Exception):
    """The webhook failed signature verification or was malformed."""


class SendLedger(Protocol):
    """Idempotency ledger. Production needs a durable implementation (shared DB or file)."""

    def get(self, key: str) -> SendResult | None: ...
    def put(self, key: str, result: SendResult) -> None: ...


class MemoryLedger:
    """Default in-memory ledger: lost on restart, so NOT enough for production."""

    def __init__(self) -> None:
        self._data: dict[str, SendResult] = {}

    def get(self, key: str) -> SendResult | None:
        return self._data.get(key)

    def put(self, key: str, result: SendResult) -> None:
        self._data[key] = result


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


class TwilioTelephony:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        client: Any | None = None,
        webhook_url: str | None = None,
        ledger: SendLedger | None = None,
    ) -> None:
        self.from_number = from_number
        self.webhook_url = webhook_url
        self.ledger: SendLedger = ledger if ledger is not None else MemoryLedger()
        self._client = client or Client(
            account_sid, auth_token, http_client=TwilioHttpClient(timeout=15)
        )
        self._validator = RequestValidator(auth_token)
        self._lock = asyncio.Lock()

    async def send_sms(self, to: str, body: str, idempotency_key: str) -> SendResult:
        """Send once per key. Twilio has no idempotency key, so the ledger is the guard.

        A timeout or dropped connection after sending returns "uncertain" and is
        recorded; the same key never triggers a second request.
        """
        async with self._lock:
            prior = self.ledger.get(idempotency_key)
            if prior is not None:
                return prior
            result = await self._send(to, body)
            self.ledger.put(idempotency_key, result)
            return result

    async def _send(self, to: str, body: str) -> SendResult:
        try:
            msg = await asyncio.to_thread(
                self._client.messages.create, to=to, from_=self.from_number, body=body
            )
        except TwilioRestException as exc:  # Twilio answered with an error: not sent
            return SendResult(status="failed", error=f"{exc.status}: {exc.msg}")
        except requests.exceptions.ConnectTimeout as exc:  # never connected: not sent
            return SendResult(status="failed", error=str(exc))
        except (requests.exceptions.RequestException, TwilioException, OSError) as exc:
            return SendResult(status="uncertain", error=f"{type(exc).__name__}: {exc}")
        return SendResult(status="sent", provider_id=str(msg.sid))

    def parse_webhook(self, request: Any) -> TelephonyEvent:
        """Verify X-Twilio-Signature, then map the form to an event."""
        try:
            form: Mapping[str, str] = request["form"]
            headers: Mapping[str, str] = request.get("headers", {})
            # Twilio signs the public URL it called; behind a proxy the app sees another one.
            url: str = self.webhook_url or request.get("url") or ""
        except (TypeError, KeyError, AttributeError) as exc:
            raise WebhookRejected("malformed request") from exc
        signature = _header(headers, "X-Twilio-Signature")
        if not signature or not url or not self._validator.validate(url, dict(form), signature):
            raise WebhookRejected("invalid signature")
        return self._to_event(form)

    @staticmethod
    def _to_event(form: Mapping[str, str]) -> TelephonyEvent:
        sender, to = form.get("From", ""), form.get("To", "")
        if "Body" in form and "MessageSid" in form:
            return TelephonyEvent(
                kind="inbound_sms",
                from_number=sender,
                to_number=to,
                body=form["Body"],
                provider_id=form["MessageSid"],
                at=datetime.now(UTC),
            )
        missed = form.get("CallStatus") in MISSED_STATUSES
        return TelephonyEvent(
            kind="missed_call" if missed else "other",
            from_number=sender,
            to_number=to,
            provider_id=form.get("CallSid") or form.get("MessageSid"),
            at=datetime.now(UTC),
        )


def create() -> TwilioTelephony:
    """Build from BIZ_TWILIO_SID / BIZ_TWILIO_TOKEN / BIZ_TWILIO_FROM / BIZ_TWILIO_WEBHOOK_URL."""
    return TwilioTelephony(
        os.environ["BIZ_TWILIO_SID"],
        os.environ["BIZ_TWILIO_TOKEN"],
        os.environ["BIZ_TWILIO_FROM"],
        webhook_url=os.environ.get("BIZ_TWILIO_WEBHOOK_URL"),  # full public URL Twilio calls
    )
