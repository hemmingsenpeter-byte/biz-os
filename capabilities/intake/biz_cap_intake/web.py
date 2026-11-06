"""Public intake routes (mounted at /webhooks: /webhooks/telephony, /webhooks/form) and the
mail poller."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import time
from collections import deque
from datetime import UTC, timedelta
from typing import Any

import sqlalchemy as sa
from biz_kernel import db
from biz_kernel.auth.deps import DbSession
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from biz_cap_intake import config, service
from biz_cap_intake.models import Cursor

log = logging.getLogger(__name__)


class FormIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    subject: str = Field(default="", max_length=300)
    message: str = Field(min_length=1, max_length=5000)
    website: str = Field(default="", max_length=200)  # honeypot: humans leave it empty


class RateLimiter:
    """Sliding window per client key, in process, with bounded memory. A reverse proxy should
    also limit."""

    def __init__(self, limit: int = 10, window: float = 60.0, max_keys: int = 10_000) -> None:
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._hits: dict[str, deque[float]] = {}

    def _evict(self, now: float) -> None:
        for k in [k for k, h in self._hits.items() if not h or now - h[-1] > self.window]:
            del self._hits[k]
        while len(self._hits) >= self.max_keys:  # still full: drop the oldest-inserted key
            del self._hits[next(iter(self._hits))]

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.pop(key, None)  # re-inserted below, so dict order is recency order
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._evict(now)
            hits = deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        allowed = len(hits) < self.limit
        if allowed:
            hits.append(now)
        if hits:
            self._hits[key] = hits
        return allowed


DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


def _trusted_networks() -> list[Any]:
    raw = os.environ.get("BIZ_TRUSTED_PROXIES") or config.setting(
        "trusted_proxies", DEFAULT_TRUSTED_PROXIES
    )
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    nets = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            log.warning("ignoring bad trusted proxy %r", item)
    return nets


def _valid_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


def client_ip(request: Request, trusted: list[Any] | None = None) -> str:
    """The caller's IP. Proxy headers (CF-Connecting-IP, then the last X-Forwarded-For hop
    added by our own proxy) count only when the direct peer is a configured trusted proxy."""
    peer = request.client.host if request.client else "unknown"
    peer_ip = _valid_ip(peer)
    if peer_ip is None:
        return peer
    nets = _trusted_networks() if trusted is None else trusted
    if not any(ipaddress.ip_address(peer_ip) in n for n in nets):
        return peer_ip
    cf = request.headers.get("cf-connecting-ip")
    if cf and _valid_ip(cf):
        return _valid_ip(cf) or peer_ip
    xff = request.headers.get("x-forwarded-for")
    if xff:
        last = _valid_ip(xff.split(",")[-1])
        if last:
            return last
    return peer_ip


async def _payload(request: Request) -> dict[str, Any]:
    """The webhook contract every TelephonyPort parses: {"form", "headers", "url"}."""
    ctype = request.headers.get("content-type", "")
    data: dict[str, Any]
    if "json" in ctype:
        body = await request.json()
        data = dict(body) if isinstance(body, dict) else {}
    else:
        data = {k: str(v) for k, v in (await request.form()).items()}
    return {"form": data, "headers": dict(request.headers), "url": str(request.url)}


def build_router(rt: Any, limiter: RateLimiter | None = None) -> APIRouter:
    router = APIRouter()
    limiter = limiter or RateLimiter()
    telephony = rt.ports["telephony"]

    @router.post("/telephony")
    async def telephony_webhook(request: Request, session: DbSession) -> dict[str, Any]:
        data = await _payload(request)
        try:
            event = telephony.parse_webhook(data)
        except Exception as exc:  # bad signature or malformed: say nothing about which
            log.warning("telephony webhook rejected: %s", type(exc).__name__)
            raise HTTPException(403, "rejected") from exc
        if event.kind == "other":
            return {"status": "ignored"}
        channel = "phone" if event.kind == "missed_call" else "sms"
        got = service.receive(
            session,
            rt.bus,
            channel=channel,
            sender=event.from_number,
            body=event.body or "",
            provider_id=event.provider_id,
        )
        return {
            "status": "duplicate" if got.duplicate else "accepted",
            "enquiry_id": str(got.enquiry.id),
        }

    @router.post("/form", status_code=202)
    def form(body: FormIn, request: Request, session: DbSession) -> dict[str, Any]:
        client = client_ip(request)
        if not limiter.allow(client):
            raise HTTPException(429, "slow down", headers={"Retry-After": "60"})
        if body.website:  # honeypot filled: pretend success, store nothing
            return {"status": "accepted"}
        sender = body.email.strip() or body.phone.strip()
        if not sender:
            raise HTTPException(422, "email or phone is required")
        digest = hashlib.sha256(f"{sender}\n{body.message}".encode()).hexdigest()[:32]
        got = service.receive(
            session,
            rt.bus,
            channel="form",
            sender=sender,
            subject=body.subject or (f"Form from {body.name}" if body.name else "Form enquiry"),
            body=body.message,
            provider_id=f"form:{digest}",
        )
        return {"status": "accepted", "enquiry_id": str(got.enquiry.id)}

    return router


_REF = re.compile(r"\[ref:([0-9a-f]{6,24})\]", re.I)


def match_reply(session: Any, sender: str, subject: str) -> dict[str, Any] | None:
    """Our sent message this mail replies to, found by the [ref:...] token in the subject.

    The sender must be the address we wrote to (normalised); otherwise it is not a reply to
    that thread, so someone else cannot hijack it. Reads the comms outbox by SQL: capabilities
    never import each other.
    """
    ref = _REF.search(subject)
    if ref is None:
        return None
    row = (
        session.execute(
            sa.text(
                'select id, "to", expected_to, party_id, meta from cap_comms_outbox '
                "where reply_token = :t and channel = 'mail' and status = 'sent'"
            ),
            {"t": ref.group(1).lower()},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    want = service.normalize_address(row["expected_to"] or row["to"])
    if service.normalize_address(sender) != want:
        return None
    return {
        "message_id": str(row["id"]),
        "party_id": str(row["party_id"] or ""),
        "enquiry_id": str((row["meta"] or {}).get("enquiry_id") or ""),
    }


def receive_reply(session: Any, rt: Any, m: Any, thread: dict[str, Any]) -> Any:
    """Publish reply.received once per provider message id."""
    sender = service.normalize_address(str(m.sender or ""))[: service.MAX_SENDER]
    subject = str(m.subject or "")[: service.MAX_SUBJECT]
    body = str(m.body or "")[: service.MAX_BODY]
    payload: dict[str, Any] = {
        "channel": "mail",
        "sender": sender,
        "from": sender,
        "subject": subject,
        "body": body,
        "message_id": thread["message_id"],
        "message_ref": thread["message_id"],
        "enquiry_id": thread["enquiry_id"],
    }
    if thread["party_id"]:
        payload["party_id"] = thread["party_id"]
    return rt.bus.publish(
        session,
        type="reply.received",
        from_part="intake",
        summary=f"Reply from {sender}: {subject}"[:200],
        payload=payload,
        idempotency_key=f"reply.received:{str(m.provider_id)[: service.MAX_PROVIDER_ID]}",
    )


def poll_mail(rt: Any) -> int:
    """Pull new mail and publish enquiry.received for each message once (by provider id).

    Each message runs in its own savepoint, so one bad row is logged and skipped instead of
    blocking the batch. The cursor advances to the latest message that was processed.
    """
    mail = rt.ports["mail"]
    lookback = timedelta(hours=float(config.setting("mail_lookback_hours", 24)))
    with db.session_scope() as s:
        cur = s.get(Cursor, "mail")
        since = cur.value if cur else db.utcnow() - lookback
    messages = asyncio.run(mail.fetch_new(since))
    new = 0
    with db.session_scope() as s:
        latest = since
        has_outbox = sa.inspect(s.connection()).has_table("cap_comms_outbox")
        for m in sorted(messages, key=lambda m: m.received_at):
            try:
                with s.begin_nested():
                    thread = (
                        match_reply(s, str(m.sender or ""), str(m.subject or ""))
                        if has_outbox
                        else None
                    )
                    if thread is not None:
                        receive_reply(s, rt, m, thread)
                        at = (
                            m.received_at
                            if m.received_at.tzinfo
                            else m.received_at.replace(tzinfo=UTC)
                        )
                        latest = max(latest, at)
                        continue
                    got = service.receive(
                        s,
                        rt.bus,
                        channel="mail",
                        sender=str(m.sender or "")[: service.MAX_SENDER],
                        subject=str(m.subject or "")[: service.MAX_SUBJECT],
                        body=str(m.body or "")[: service.MAX_BODY],
                        provider_id=str(m.provider_id)[: service.MAX_PROVIDER_ID],
                    )
            except Exception:
                log.exception("mail message %r skipped", str(m.provider_id)[:80])
                continue
            new += 0 if got.duplicate else 1
            at = m.received_at if m.received_at.tzinfo else m.received_at.replace(tzinfo=UTC)
            latest = max(latest, at)
        cur = s.get(Cursor, "mail")
        if cur is None:
            s.add(Cursor(name="mail", value=latest))
        elif latest > cur.value:
            cur.value = latest
    return new
