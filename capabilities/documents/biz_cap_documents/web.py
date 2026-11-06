"""Public webhook (mounted at /webhooks/documents): DocuSeal signature events."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from biz_kernel.audit.log import record
from biz_kernel.auth.deps import DbSession
from biz_kernel.db import utcnow
from biz_kernel.records.models import Document
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_documents import service
from biz_cap_documents.models import Render, Signature

log = logging.getLogger(__name__)
SECRET_ENV = "BIZ_DOCUSEAL_WEBHOOK_SECRET"
HEADER_ENV = "BIZ_DOCUSEAL_WEBHOOK_HEADER"
DEFAULT_HEADER = "X-Docuseal-Secret"
MAX_PDF = 25 * 1024 * 1024

Fetch = Callable[[str], bytes | None]


def secret_ok(headers: Any) -> bool:
    """Constant-time check of the shared secret; an unset secret rejects everything."""
    secret = os.environ.get(SECRET_ENV, "")
    given = headers.get(os.environ.get(HEADER_ENV, DEFAULT_HEADER), "")
    return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())


def default_fetch(url: str) -> bytes | None:
    """Download a signed PDF, only from the configured DocuSeal host, never following redirects."""
    base = os.environ.get("BIZ_DOCUSEAL_URL", "")
    if not base or urlparse(url).netloc != urlparse(base).netloc:
        log.warning("signed pdf url is not on the DocuSeal host; not fetched")
        return None
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=False)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("signed pdf fetch failed: %s", type(exc).__name__)
        return None
    return resp.content if len(resp.content) <= MAX_PDF else None


def _pdf_url(body: dict[str, Any]) -> str | None:
    data = body.get("data") or {}
    for doc in data.get("documents") or []:
        if isinstance(doc, dict) and isinstance(doc.get("url"), str):
            return str(doc["url"])
    return None


def _store_signed(
    session: Session, storage: Any, sig: Signature, original: Document, blob: bytes
) -> Document:
    doc, _ = service.add_version(
        session,
        storage,
        data=blob,
        name=f"{original.title}-signed.pdf",
        content_type="application/pdf",
        fmt="pdf",
        content_hash=service.sha256(blob),
        job_id=original.job_id,
        kind=original.kind,
        title=original.title,
    )
    return doc


def apply_event(
    session: Session,
    rt: Any,
    event: Any,
    pdf_url: str | None,
    fetch: Fetch,
) -> str:
    """Update the signature row for an event. Returns the resulting status word."""
    sig = session.scalar(
        select(Signature).where(Signature.request_id == event.request_id).with_for_update()
    )
    if sig is None:
        return "unknown"
    if sig.status != "requested":
        return "duplicate"  # terminal states are final; replays change nothing
    if event.kind == "declined":
        sig.status, sig.resolved_at = "declined", utcnow()
        record(session, "documents", "documents.declined", str(sig.id), request_id=sig.request_id)
        session.flush()
        return "declined"
    if event.kind == "signed":
        who = (event.signer_email or "").lower()
        done = {str(e).lower() for e in sig.signed_by}
        if who and who not in done:
            sig.signed_by = [*sig.signed_by, who]
        wanted = {str(s["email"]).lower() for s in sig.signers}
        if not wanted <= {str(e).lower() for e in sig.signed_by}:
            session.flush()
            return "progress"
    elif event.kind != "completed":
        return "ignored"
    original = session.get(Document, sig.document_id)
    assert original is not None
    sig.status, sig.resolved_at = "signed", utcnow()
    signed_id = None
    blob = fetch(pdf_url) if pdf_url else None
    if blob is not None and "storage" in rt.ports:
        signed_id = _store_signed(session, rt.ports["storage"], sig, original, blob).id
        sig.signed_document_id = signed_id
    signed_at: datetime = event.at or utcnow()
    payload: dict[str, Any] = {
        "document_id": str(original.id),
        "request_id": sig.request_id,
        "signed_at": signed_at.isoformat(),
    }
    if signed_id:
        payload["signed_document_id"] = str(signed_id)
    rt.bus.publish(
        session,
        type="document.signed",
        payload=payload,
        from_part="documents",
        job_id=original.job_id,
        summary=f"{original.title} signed",
        evidence={"signature_id": str(sig.id), "document_sha256": original.sha256},
        idempotency_key=f"document.signed:{sig.request_id}",
    )
    session.flush()
    return "signed"


def build_router(rt: Any, fetch: Fetch | None = None) -> APIRouter:
    router = APIRouter()
    get_pdf = fetch or default_fetch

    @router.post("/documents/sign")
    async def sign_webhook(request: Request, session: DbSession) -> dict[str, Any]:
        if not secret_ok(request.headers):
            raise HTTPException(403, "rejected")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("not an object")
            contract = {"form": body, "headers": dict(request.headers), "url": str(request.url)}
            event = rt.ports["sign"].parse_webhook(contract)
        except Exception as exc:  # malformed body: say nothing about why
            log.warning("sign webhook rejected: %s", type(exc).__name__)
            raise HTTPException(403, "rejected") from exc
        status = apply_event(session, rt, event, _pdf_url(body), get_pdf)
        return {"status": status}

    return router


__all__ = ["Render", "apply_event", "build_router", "secret_ok"]
