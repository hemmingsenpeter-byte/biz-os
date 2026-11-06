"""Microsoft 365 adapter (Graph REST over httpx) for MailPort and CalendarPort.

Implemented against Graph REST with httpx rather than msgraph-sdk: the SDK's kiota
request-adapter stack is hard to mock and adds nothing for these five calls.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from biz_kernel.contracts.ports import (
    CalendarEvent,
    ExternalRef,
    InboundMessage,
    OutboundMessage,
    SendResult,
    Slot,
)

__all__ = ["IDEMPOTENCY_HEADER", "M365Mail", "MemoryLedger", "SendLedger", "create"]

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
IDEMPOTENCY_HEADER = "X-Biz-Idempotency-Key"
TOKEN_SKEW_S = 60
SENT_ITEMS_PAGES = 5


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


def _parse_graph_dt(value: str) -> datetime:
    """Graph returns e.g. 2030-01-07T09:00:00.0000000 (7 fraction digits, UTC if asked)."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.rstrip("Z"))
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _graph_dt(value: datetime) -> dict[str, str]:
    return {"dateTime": value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class M365Mail:
    """MailPort + CalendarPort for one mailbox, using app-only (client credentials) auth."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        http: httpx.AsyncClient | None = None,
        ledger: SendLedger | None = None,
        timezone: str = "UTC",
        work_start: dtime = dtime(9, 0),
        work_end: dtime = dtime(17, 0),
        workdays: tuple[int, ...] = (0, 1, 2, 3, 4),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.mailbox = mailbox
        self.http = http or httpx.AsyncClient(timeout=30)
        self.ledger: SendLedger = ledger if ledger is not None else MemoryLedger()
        self.tz = ZoneInfo(timezone)
        self.work_start, self.work_end, self.workdays = work_start, work_end, workdays
        self._clock = clock
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    # -- auth / transport -------------------------------------------------
    async def _get_token(self) -> str:
        async with self._token_lock:
            if self._token and self._clock() < self._token_expiry:
                return self._token
            resp = await self.http.post(
                f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._token_expiry = self._clock() + int(body.get("expires_in", 3600)) - TOKEN_SKEW_S
            return self._token

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{GRAPH}/users/{self.mailbox}{url}"
        headers = {"Authorization": f"Bearer {await self._get_token()}", **kw.pop("headers", {})}
        return await self.http.request(method, url, headers=headers, **kw)

    # -- MailPort -----------------------------------------------------------
    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult:
        """Send once per key. An unconfirmed send is "uncertain" and reconciled, never resent."""
        async with self._send_lock:
            prior = self.ledger.get(idempotency_key)
            if prior is not None:
                if prior.status != "uncertain":
                    return prior
                found = await self._safe_reconcile(idempotency_key)
                if found is None:
                    return prior
                self.ledger.put(idempotency_key, found)
                return found
            result = await self._send(msg, idempotency_key)
            self.ledger.put(idempotency_key, result)
            return result

    async def _safe_reconcile(self, key: str) -> SendResult | None:
        try:
            return await self.reconcile(key)
        except httpx.HTTPError:
            return None

    async def _send(self, msg: OutboundMessage, key: str) -> SendResult:
        def addr(a: str) -> dict[str, Any]:
            return {"emailAddress": {"address": a}}

        message: dict[str, Any] = {
            "subject": msg.subject,
            "body": {"contentType": "Text", "content": msg.body},
            "toRecipients": [addr(a) for a in msg.to],
            "ccRecipients": [addr(a) for a in msg.cc],
            "internetMessageHeaders": [{"name": IDEMPOTENCY_HEADER, "value": key}],
        }
        if msg.in_reply_to:
            message["internetMessageHeaders"].append(
                {"name": "X-Biz-In-Reply-To", "value": msg.in_reply_to}
            )
        try:
            resp = await self._request(
                "POST", "/sendMail", json={"message": message, "saveToSentItems": True}
            )
        except httpx.ConnectTimeout as exc:  # never connected: not sent
            return SendResult(status="failed", error=str(exc))
        except httpx.HTTPError as exc:  # request may have reached Graph
            return SendResult(status="uncertain", error=f"{type(exc).__name__}: {exc}")
        if resp.status_code == 202:
            return SendResult(status="sent", provider_id=f"m365:{key}")
        if resp.status_code >= 500:
            return SendResult(status="uncertain", error=f"HTTP {resp.status_code}")
        return SendResult(status="failed", error=f"HTTP {resp.status_code}: {resp.text[:200]}")

    async def reconcile(self, idempotency_key: str) -> SendResult | None:
        """Look in Sent Items for the idempotency header. None if no such message."""
        url: str | None = (
            "/mailFolders/sentitems/messages?$top=50&$orderby=sentDateTime desc"
            "&$select=id,sentDateTime,internetMessageHeaders"
        )
        for _ in range(SENT_ITEMS_PAGES):
            if url is None:
                break
            resp = await self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("value", []):
                for h in m.get("internetMessageHeaders") or []:
                    if (
                        h.get("name", "").lower() == IDEMPOTENCY_HEADER.lower()
                        and h.get("value") == idempotency_key
                    ):
                        return SendResult(status="sent", provider_id=m["id"])
            url = data.get("@odata.nextLink")
        return None

    async def fetch_new(self, since: datetime) -> list[InboundMessage]:
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        url: str | None = (
            f"/mailFolders/inbox/messages?$filter=receivedDateTime gt {stamp}"
            "&$orderby=receivedDateTime asc&$top=50"
            "&$select=id,subject,body,from,toRecipients,receivedDateTime,conversationId"
        )
        out: list[InboundMessage] = []
        while url:
            resp = await self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("value", []):
                body = m.get("body") or {}
                text = body.get("content", "")
                if body.get("contentType") == "html":
                    text = _strip_html(text)
                out.append(
                    InboundMessage(
                        provider_id=m["id"],
                        sender=((m.get("from") or {}).get("emailAddress") or {}).get("address", ""),
                        to=[r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
                        subject=m.get("subject") or "",
                        body=text,
                        received_at=_parse_graph_dt(m["receivedDateTime"]),
                        thread_id=m.get("conversationId"),
                    )
                )
            url = data.get("@odata.nextLink")
        return out

    # -- CalendarPort ---------------------------------------------------------
    async def free_slots(self, start: datetime, end: datetime, duration_minutes: int) -> list[Slot]:
        """Slots of `duration_minutes` inside working hours that don't overlap busy time."""
        resp = await self._request(
            "POST",
            "/calendar/getSchedule",
            json={
                "schedules": [self.mailbox],
                "startTime": _graph_dt(start),
                "endTime": _graph_dt(end),
                "availabilityViewInterval": 15,
            },
        )
        resp.raise_for_status()
        busy: list[tuple[datetime, datetime]] = []
        for sched in resp.json().get("value", []):
            for item in sched.get("scheduleItems", []):
                if item.get("status", "busy") != "free":
                    busy.append(
                        (
                            _parse_graph_dt(item["start"]["dateTime"]),
                            _parse_graph_dt(item["end"]["dateTime"]),
                        )
                    )
        step = timedelta(minutes=duration_minutes)
        slots: list[Slot] = []
        day = start.astimezone(self.tz).date() - timedelta(days=1)
        last = end.astimezone(self.tz).date() + timedelta(days=1)
        while day <= last:
            if day.weekday() in self.workdays:
                cur = datetime.combine(day, self.work_start, self.tz).astimezone(UTC)
                close = datetime.combine(day, self.work_end, self.tz).astimezone(UTC)
                while cur + step <= close:
                    nxt = cur + step
                    if (
                        cur >= start
                        and nxt <= end
                        and not any(b0 < nxt and cur < b1 for b0, b1 in busy)
                    ):
                        slots.append(Slot(start=cur, end=nxt))
                    cur = nxt
            day += timedelta(days=1)
        return slots

    async def book(self, event: CalendarEvent, idempotency_key: str) -> ExternalRef:
        """Create an event; Graph's transactionId makes a repeat of the same key a no-op."""
        payload: dict[str, Any] = {
            "subject": event.title,
            "start": _graph_dt(event.start),
            "end": _graph_dt(event.end),
            "attendees": [
                {"emailAddress": {"address": a}, "type": "required"} for a in event.attendees
            ],
            "transactionId": idempotency_key,
        }
        if event.location:
            payload["location"] = {"displayName": event.location}
        if event.body:
            payload["body"] = {"contentType": "Text", "content": event.body}
        resp = await self._request("POST", "/calendar/events", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return ExternalRef(system="m365", id=data["id"], url=data.get("webLink"))


def create() -> M365Mail:
    """Build from BIZ_M365_TENANT / _CLIENT_ID / _SECRET / _MAILBOX (+ optional BIZ_M365_TZ,
    BIZ_M365_WORK_START, BIZ_M365_WORK_END as HH:MM)."""
    env = os.environ
    return M365Mail(
        env["BIZ_M365_TENANT"],
        env["BIZ_M365_CLIENT_ID"],
        env["BIZ_M365_SECRET"],
        env["BIZ_M365_MAILBOX"],
        timezone=env.get("BIZ_M365_TZ", "UTC"),
        work_start=dtime.fromisoformat(env.get("BIZ_M365_WORK_START", "09:00")),
        work_end=dtime.fromisoformat(env.get("BIZ_M365_WORK_END", "17:00")),
    )
