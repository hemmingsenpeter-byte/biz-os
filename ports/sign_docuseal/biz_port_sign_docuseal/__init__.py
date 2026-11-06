"""DocuSeal adapter for SignPort (DocuSeal runs as an unmodified service; REST over httpx).

`request_signature` creates a submission. With a `document_loader` the document's PDF is
uploaded (POST /api/submissions/pdf); otherwise `template_id` selects a DocuSeal template
(POST /api/submissions). A durable ledger guards against creating two submissions (and so
two sets of emails) for the same document and signers.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from biz_kernel.contracts.ports import ExternalRef, Signer, SignEvent
from biz_kernel.db import engine as _db_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .models import DocuSealRequest

__all__ = [
    "DocuSealSign",
    "MemoryLedger",
    "RequestLedger",
    "SignRequestFailed",
    "SignRequestUncertain",
    "SqlLedger",
    "WebhookRejected",
    "create",
]

# (name, pdf bytes) for a document id, supplied by the composition.
DocumentLoader = Callable[[uuid.UUID], tuple[str, bytes]]


class WebhookRejected(Exception):
    """The webhook body was malformed."""


class SignRequestFailed(Exception):
    """DocuSeal refused the request; nothing was created, so it may be retried."""


class SignRequestUncertain(Exception):
    """The request may or may not have reached DocuSeal; never retried automatically."""


class RequestLedger(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def claim(self, key: str) -> bool: ...
    def put(self, key: str, provider_id: str, url: str | None) -> None: ...
    def release(self, key: str) -> None: ...


class MemoryLedger:
    """In-memory ledger: lost on restart, so NOT enough for production."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = {"status": "sending", "provider_id": None, "url": None}
            return True

    def put(self, key: str, provider_id: str, url: str | None) -> None:
        with self._lock:
            self._data[key] = {"status": "sent", "provider_id": provider_id, "url": url}

    def release(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class SqlLedger:
    """Durable ledger on port_docuseal_requests; "sending" is recorded before the call."""

    def __init__(self, engine_or_factory: Any) -> None:
        self._factory: Any = (
            sessionmaker(engine_or_factory, expire_on_commit=False)
            if isinstance(engine_or_factory, Engine)
            else engine_or_factory
        )

    def get(self, key: str) -> dict[str, Any] | None:
        with self._factory() as s:
            row = s.get(DocuSealRequest, key)
        if row is None:
            return None
        return {
            "status": row.status,
            "provider_id": row.provider_id,
            "url": (row.result or {}).get("url"),
        }

    def claim(self, key: str) -> bool:
        with self._factory() as s:
            stmt = (
                pg_insert(DocuSealRequest)
                .values(key=key, status="sending", at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(DocuSealRequest.key)
            )
            won = s.execute(stmt).scalar() is not None
            s.commit()
        return won

    def put(self, key: str, provider_id: str, url: str | None) -> None:
        with self._factory() as s:
            row = s.get(DocuSealRequest, key)
            if row is None:
                row = DocuSealRequest(key=key)
                s.add(row)
            row.status, row.provider_id, row.result = "sent", provider_id, {"url": url}
            row.at = datetime.now(UTC)
            s.commit()

    def release(self, key: str) -> None:
        with self._factory() as s:
            row = s.get(DocuSealRequest, key)
            if row is not None and row.status == "sending":
                s.delete(row)
                s.commit()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _request_key(doc_id: uuid.UUID, signers: list[Signer]) -> str:
    who = ",".join(sorted(f"{s.email.lower()}:{s.role}" for s in signers))
    return f"{doc_id}:{hashlib.sha256(who.encode()).hexdigest()[:16]}"


class DocuSealSign:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: httpx.AsyncClient | None = None,
        template_id: int | None = None,
        document_loader: DocumentLoader | None = None,
        ledger: RequestLedger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.template_id = template_id
        self.document_loader = document_loader
        self.ledger: RequestLedger = ledger if ledger is not None else MemoryLedger()
        self._injected_http = http

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        headers = {"X-Auth-Token": self.api_key}
        url = f"{self.base_url}{path}"
        if self._injected_http is not None:
            return await self._injected_http.post(url, json=body, headers=headers)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, json=body, headers=headers)

    def _body(self, doc_id: uuid.UUID, signers: list[Signer]) -> tuple[str, dict[str, Any]]:
        submitters = [{"name": s.name, "email": s.email, "role": s.role} for s in signers]
        if self.document_loader is not None:
            name, data = self.document_loader(doc_id)
            return "/api/submissions/pdf", {
                "name": name,
                "send_email": True,
                "documents": [{"name": name, "file": base64.b64encode(data).decode()}],
                "submitters": submitters,
            }
        if self.template_id is None:
            raise SignRequestFailed("no document loader or template_id configured")
        return "/api/submissions", {
            "template_id": self.template_id,
            "send_email": True,
            "submitters": submitters,
        }

    async def request_signature(self, doc_id: uuid.UUID, signers: list[Signer]) -> ExternalRef:
        key = _request_key(doc_id, signers)
        prior = self.ledger.get(key)
        if prior is None:
            if self.ledger.claim(key):
                return await self._create(key, doc_id, signers)
            prior = self.ledger.get(key)
        if prior is None or prior["status"] != "sent":
            raise SignRequestUncertain("a request for this document is in flight or crashed")
        return ExternalRef(system="docuseal", id=prior["provider_id"], url=prior.get("url"))

    async def _create(self, key: str, doc_id: uuid.UUID, signers: list[Signer]) -> ExternalRef:
        try:
            path, body = self._body(doc_id, signers)
        except BaseException:
            self.ledger.release(key)
            raise
        try:
            resp = await self._post(path, body)
        except httpx.ConnectTimeout as exc:  # never connected: nothing created
            self.ledger.release(key)
            raise SignRequestFailed(str(exc)) from exc
        except httpx.HTTPError as exc:  # may have reached DocuSeal: stays "sending"
            raise SignRequestUncertain(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            self.ledger.release(key)
            raise SignRequestFailed(f"{resp.status_code}: {resp.text[:200]}")
        try:
            sub_id, url = self._parse_created(resp.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SignRequestUncertain("unreadable DocuSeal response") from exc
        self.ledger.put(key, sub_id, url)
        return ExternalRef(system="docuseal", id=sub_id, url=url)

    @staticmethod
    def _parse_created(data: Any) -> tuple[str, str | None]:
        """/submissions returns a list of submitters; /submissions/pdf an object with `id`."""
        if isinstance(data, dict) and "id" in data:
            subs = data.get("submitters") or [{}]
            first = subs[0]
            sub_id = data["id"]
        else:
            first = data[0]
            sub_id = first["submission_id"]
        url = first.get("embed_src") or (f"/s/{first['slug']}" if first.get("slug") else None)
        return str(sub_id), url

    def parse_webhook(self, request: Any) -> SignEvent:
        """Map a DocuSeal webhook body to an event. The caller verifies the shared secret."""
        try:
            body = request.get("form", request)
            event_type = str(body["event_type"])
            data = body.get("data") or {}
        except (TypeError, KeyError, AttributeError) as exc:
            raise WebhookRejected("malformed request") from exc
        if event_type.startswith("submission."):
            request_id = data.get("id")
        else:
            request_id = (data.get("submission") or {}).get("id") or data.get("submission_id")
        if request_id is None:
            raise WebhookRejected("no submission id")
        kind: Any = {
            "form.completed": "signed",
            "form.viewed": "viewed",
            "form.declined": "declined",
            "submission.completed": "completed",
            "submission.declined": "declined",
        }.get(event_type, "other")
        return SignEvent(
            kind=kind,
            request_id=str(request_id),
            signer_email=data.get("email") if not event_type.startswith("submission.") else None,
            at=_when(body.get("timestamp")),
        )


def _when(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def create() -> DocuSealSign:
    """Build from BIZ_DOCUSEAL_URL / BIZ_DOCUSEAL_API_KEY (+ optional BIZ_DOCUSEAL_TEMPLATE_ID).

    Documents are uploaded from the `documents` table through BIZ_STORAGE_ROOT.
    """
    template = os.environ.get("BIZ_DOCUSEAL_TEMPLATE_ID")
    return DocuSealSign(
        os.environ["BIZ_DOCUSEAL_URL"],
        os.environ["BIZ_DOCUSEAL_API_KEY"],
        template_id=int(template) if template else None,
        document_loader=_db_loader() if os.environ.get("BIZ_STORAGE_ROOT") else None,
        ledger=SqlLedger(_db_engine()),
    )


def _db_loader() -> DocumentLoader:
    from pathlib import Path
    from urllib.parse import unquote, urlparse

    from biz_kernel.db import session_scope
    from biz_kernel.records.models import Document

    root = Path(os.environ["BIZ_STORAGE_ROOT"]).resolve()

    def load(doc_id: uuid.UUID) -> tuple[str, bytes]:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                raise SignRequestFailed(f"document {doc_id} not found")
            title, uri = doc.title, doc.uri
        path = Path(unquote(urlparse(uri).path)).resolve()
        if urlparse(uri).scheme != "file" or not path.is_relative_to(root):
            raise SignRequestFailed("document is not in local storage")
        return title, path.read_bytes()

    return load
