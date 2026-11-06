"""Public intake routes (mounted at /v1/intake) and the mail poller."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict, deque
from datetime import UTC, timedelta
from typing import Any

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
    """Sliding window per client key, in process. A reverse proxy should also limit."""

    def __init__(self, limit: int = 10, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


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
        client = request.client.host if request.client else "unknown"
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


def poll_mail(rt: Any) -> int:
    """Pull new mail and publish enquiry.received for each message once (by provider id)."""
    mail = rt.ports["mail"]
    lookback = timedelta(hours=float(config.setting("mail_lookback_hours", 24)))
    with db.session_scope() as s:
        cur = s.get(Cursor, "mail")
        since = cur.value if cur else db.utcnow() - lookback
    messages = asyncio.run(mail.fetch_new(since))
    new = 0
    with db.session_scope() as s:
        latest = since
        for m in sorted(messages, key=lambda m: m.received_at):
            got = service.receive(
                s,
                rt.bus,
                channel="mail",
                sender=m.sender,
                subject=m.subject,
                body=m.body,
                provider_id=m.provider_id,
            )
            new += 0 if got.duplicate else 1
            at = m.received_at if m.received_at.tzinfo else m.received_at.replace(tzinfo=UTC)
            latest = max(latest, at)
        cur = s.get(Cursor, "mail")
        if cur is None:
            s.add(Cursor(name="mail", value=latest))
        else:
            cur.value = latest
    return new
