"""Public webhook (mounted at /webhooks/documents): DocuSeal signature events."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Awaitable, Callable
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
PENDING = "signed_pending_copy"

Fetch = Callable[[str], Awaitable[bytes | None]]


def secret_ok(headers: Any) -> bool:
    """Constant-time check of the shared secret; an unset secret rejects everything."""
    secret = os.environ.get(SECRET_ENV, "")
    given = headers.get(os.environ.get(HEADER_ENV, DEFAULT_HEADER), "")
    return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())


async def default_fetch(url: str) -> bytes | None:
    """Download a signed PDF, only from the configured DocuSeal host, never following redirects.

    Streamed with a size cap so an oversized body is never buffered whole.
    """
    base = os.environ.get("BIZ_DOCUSEAL_URL", "")
    if not base or urlparse(url).netloc != urlparse(base).netloc:
        log.warning("signed pdf url is not on the DocuSeal host; not fetched")
        return None
    try:
        async with (
            httpx.AsyncClient(timeout=30, follow_redirects=False) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_PDF:
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_PDF:
                    return None
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        log.warning("signed pdf fetch failed: %s", type(exc).__name__)
        return None
    return b"".join(chunks)


def _pdf_url(body: dict[str, Any]) -> str | None:
    data = body.get("data") or {}
    for doc in data.get("documents") or []:
        if isinstance(doc, dict) and isinstance(doc.get("url"), str):
            return str(doc["url"])
    return None


def _store_signed(storage: Any, session: Session, original: Document, blob: bytes) -> Document:
    """Store the signed PDF as its own Document that references (never supersedes) the original.

    The unsigned document's version chain is untouched, so "latest" does not move.
    """
    doc, _ = service.add_version(
        session,
        storage,
        data=blob,
        name=f"{original.title}-signed.pdf",
        content_type="application/pdf",
        fmt="pdf",
        content_hash=service.sha256(blob),
        job_id=original.job_id,
        kind=f"{original.kind[:53]}.signed",
        title=f"{original.title} (signed v{original.version})",
        source_document_id=original.id,
    )
    return doc


def _lock(session: Session, request_id: str) -> Signature | None:
    stmt = select(Signature).where(Signature.request_id == request_id)
    stmt = stmt.order_by(Signature.created_at.desc()).limit(1).with_for_update()
    return session.scalar(stmt)


async def apply_event(
    session: Session,
    rt: Any,
    event: Any,
    pdf_url: str | None,
    fetch: Fetch,
) -> str:
    """Update the signature row for an event (matched on the provider submission id).

    A completed signature is "signed_pending_copy" until its PDF is stored; only then is it
    "signed" and document.signed published. The row lock is not held across the network call.
    """
    sig = _lock(session, event.request_id)
    if sig is None:
        return "unknown"
    if sig.status not in ("requested", PENDING):
        return "duplicate"  # terminal states are final; replays change nothing
    if sig.status == "requested":
        if event.kind == "declined":
            sig.status, sig.resolved_at = "declined", utcnow()
            record(
                session, "documents", "documents.declined", str(sig.id), request_id=sig.request_id
            )
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
        sig.status, sig.resolved_at = PENDING, utcnow()
    elif event.kind not in ("signed", "completed"):
        return "ignored"
    sig_id, request_id = sig.id, sig.request_id
    session.flush()
    session.commit()  # release the row lock (and persist "pending") before the network call
    blob = await fetch(pdf_url) if pdf_url else None
    storage = rt.ports.get("storage")
    if blob is None and storage is not None:
        return "pending_copy"  # a later webhook carrying the PDF url finishes the job
    sig = session.get(Signature, sig_id, with_for_update=True, populate_existing=True)
    if sig is None or sig.status != PENDING:
        return "duplicate"
    original = session.get(Document, sig.document_id)
    assert original is not None
    signed: Document | None = None
    if blob is not None and storage is not None:
        signed = _store_signed(storage, session, original, blob)
        sig.signed_document_id = signed.id
    sig.status, sig.resolved_at = "signed", utcnow()
    signed_at: datetime = event.at or utcnow()
    payload: dict[str, Any] = {
        "document_id": str(original.id),
        "request_id": request_id,
        "signed_at": signed_at.isoformat(),
    }
    evidence: dict[str, Any] = {"signature_id": str(sig_id), "request_id": request_id}
    if signed is not None:
        payload["signed_document_id"] = str(signed.id)
        evidence["signed_document_sha256"] = signed.sha256
        evidence["signed_version"] = original.version
    rt.bus.publish(
        session,
        type="document.signed",
        payload=payload,
        from_part="documents",
        job_id=original.job_id,
        summary=f"{original.title} signed",
        evidence=evidence,
        idempotency_key=f"document.signed:{request_id}",
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
        status = await apply_event(session, rt, event, _pdf_url(body), get_pdf)
        return {"status": status}

    return router


__all__ = ["Render", "apply_event", "build_router", "secret_ok"]
