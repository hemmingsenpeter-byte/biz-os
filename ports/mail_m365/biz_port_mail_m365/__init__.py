"""Microsoft 365 adapter (Graph REST over httpx) for MailPort and CalendarPort.

Implemented against Graph REST with httpx rather than msgraph-sdk: the SDK's kiota
request-adapter stack is hard to mock and adds nothing for these five calls.
"""

from __future__ import annotations

import os
import re
import threading
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
from biz_kernel.db import engine as _db_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from .auth_results import trusted_auth  # noqa: E402
from .models import M365Send  # noqa: E402

__all__ = ["IDEMPOTENCY_HEADER", "M365Mail", "MemoryLedger", "SendLedger", "SqlLedger", "create"]

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
IDEMPOTENCY_HEADER = "X-Biz-Idempotency-Key"
TOKEN_SKEW_S = 60
SENT_ITEMS_PAGES = 5


class SendLedger(Protocol):
    """Idempotency ledger. Production needs a durable implementation (shared DB or file)."""

    def get(self, key: str) -> SendResult | None: ...
    def claim(self, key: str) -> bool:
        """Atomically record "sending" before the provider call; False if the key exists."""
        ...

    def put(self, key: str, result: SendResult) -> None: ...


class MemoryLedger:
    """Default in-memory ledger: lost on restart, so NOT enough for production."""

    def __init__(self) -> None:
        self._data: dict[str, SendResult] = {}
        self._sending: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str) -> SendResult | None:
        if key in self._sending:
            return SendResult(status="uncertain", error="in flight")
        return self._data.get(key)

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._data or key in self._sending:
                return False
            self._sending.add(key)
            return True

    def put(self, key: str, result: SendResult) -> None:
        with self._lock:
            self._data[key] = result
            self._sending.discard(key)


class SqlLedger:
    """Durable ledger on port_m365_sends. `claim` records "sending" BEFORE the provider is called;
    a key left in "sending" (crash mid-send) reads back as "uncertain", never resent."""

    def __init__(self, engine_or_factory: Any) -> None:
        self._factory: Any = (
            sessionmaker(engine_or_factory, expire_on_commit=False)
            if isinstance(engine_or_factory, Engine)
            else engine_or_factory
        )

    def get(self, key: str) -> SendResult | None:
        with self._factory() as s:
            row = s.get(M365Send, key)
        if row is None:
            return None
        if row.status == "sending":
            return SendResult(status="uncertain", error="crashed or in flight before result")
        return SendResult(
            status=row.status, provider_id=row.provider_id, error=(row.result or {}).get("error")
        )

    def claim(self, key: str) -> bool:
        with self._factory() as s:
            stmt = (
                pg_insert(M365Send)
                .values(key=key, status="sending", at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(M365Send.key)
            )
            won = s.execute(stmt).scalar() is not None
            s.commit()
        return won

    def put(self, key: str, result: SendResult) -> None:
        with self._factory() as s:
            stmt = pg_insert(M365Send).values(
                key=key,
                status=result.status,
                provider_id=result.provider_id,
                result={"error": result.error},
                at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "status": stmt.excluded.status,
                    "provider_id": stmt.excluded.provider_id,
                    "result": stmt.excluded.result,
                    "at": stmt.excluded.at,
                },
            )
            s.execute(stmt)
            s.commit()


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
        authserv_id: str = "",
    ) -> None:
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.mailbox = mailbox
        # An injected client (tests) is used as-is. Otherwise a client is made per call: an
        # AsyncClient is bound to the event loop it first ran on, and callers use asyncio.run.
        self._injected_http = http
        self.ledger: SendLedger = ledger if ledger is not None else MemoryLedger()
        self.tz = ZoneInfo(timezone)
        self.work_start, self.work_end, self.workdays = work_start, work_end, workdays
        self._clock = clock
        # Only Authentication-Results stamped with this authserv-id (our receiving server) is
        # trusted; empty means every inbound sender stays unverified.
        self.authserv_id = authserv_id.strip()
        self._token: str | None = None
        self._token_expiry = 0.0

    # -- auth / transport -------------------------------------------------
    async def _http_request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if self._injected_http is not None:
            return await self._injected_http.request(method, url, **kw)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(method, url, **kw)

    async def _get_token(self) -> str:
        # No lock: a racing refresh just fetches a second token (locks are loop-bound).
        if self._token and self._clock() < self._token_expiry:
            return self._token
        resp = await self._http_request(
            "POST",
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
        return await self._http_request(method, url, headers=headers, **kw)

    # -- MailPort -----------------------------------------------------------
    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult:
        """Send once per key. An unconfirmed send is "uncertain" and reconciled, never resent."""
        prior = self.ledger.get(idempotency_key)
        if prior is None:
            if self.ledger.claim(idempotency_key):  # "sending" is recorded before Graph is called
                result = await self._send(msg, idempotency_key)
                self.ledger.put(idempotency_key, result)
                return result
            prior = self.ledger.get(idempotency_key) or SendResult(status="uncertain")
        if prior.status != "uncertain":
            return prior
        found = await self._safe_reconcile(idempotency_key)
        if found is None:
            return prior
        self.ledger.put(idempotency_key, found)
        return found

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
        # `ge` and full precision: same-instant mail must not be skipped; callers dedupe on id.
        stamp = since.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        url: str | None = (
            f"/mailFolders/inbox/messages?$filter=receivedDateTime ge {stamp}"
            "&$orderby=receivedDateTime asc&$top=50"
            "&$select=id,subject,body,from,toRecipients,receivedDateTime,conversationId,"
            "internetMessageHeaders"
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
                        auth=trusted_auth(m.get("internetMessageHeaders") or [], self.authserv_id),
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
    BIZ_M365_WORK_START, BIZ_M365_WORK_END as HH:MM, and BIZ_M365_AUTHSERV_ID: the
    authserv-id of the tenant's own Authentication-Results header; unset = never verified)."""
    env = os.environ
    return M365Mail(
        env["BIZ_M365_TENANT"],
        env["BIZ_M365_CLIENT_ID"],
        env["BIZ_M365_SECRET"],
        env["BIZ_M365_MAILBOX"],
        ledger=SqlLedger(_db_engine()),
        timezone=env.get("BIZ_M365_TZ") or "UTC",
        work_start=dtime.fromisoformat(env.get("BIZ_M365_WORK_START", "09:00")),
        work_end=dtime.fromisoformat(env.get("BIZ_M365_WORK_END", "17:00")),
        authserv_id=env.get("BIZ_M365_AUTHSERV_ID", ""),
    )
