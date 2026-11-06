"""Documents tools: render and file are drafts; request_signature is external (gated)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.contracts.ports import Signer, SignPort, StoragePort
from biz_kernel.policy.types import SideEffect
from biz_kernel.records.models import Document
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_documents import config, render, service
from biz_cap_documents.models import Render, Signature

CAP = "documents"
_STR: dict[str, Any] = {"type": "string"}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": props,
    }


def _sig_view(sig: Signature, **extra: Any) -> dict[str, Any]:
    return {
        "signature_id": str(sig.id),
        "document_id": str(sig.document_id),
        "request_id": sig.request_id,
        "status": sig.status,
        "signed_document_id": str(sig.signed_document_id) if sig.signed_document_id else None,
        **extra,
    }


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    storage: StoragePort = ports["storage"]
    sign: SignPort = ports["sign"]

    def render_doc(
        session: Session,
        template: str,
        title: str,
        data: dict[str, Any] | None = None,
        kind: str = "document",
        job_id: str | None = None,
        format: str = "pdf",
    ) -> dict[str, Any]:
        path = config.resolve_template(template)
        try:
            source = render.render_html(path, data or {}, title)
        except render.TemplateFailed as exc:
            raise ValueError(str(exc)) from exc
        content_hash = service.sha256(f"{format}\n{source}")
        if format == "pdf":
            prev = service.latest(session, uuid.UUID(job_id) if job_id else None, kind, title)
            prev_render = session.get(Render, prev.id) if prev else None
            if prev and prev_render and prev_render.content_hash == content_hash:
                return service.view(prev, prev_render, deduplicated=True)  # skip the PDF work
            blob, ctype, name = render.html_to_pdf(source), "application/pdf", f"{title}.pdf"
        else:
            blob, ctype, name = source.encode(), "text/html", f"{title}.html"
        doc, created = service.add_version(
            session,
            storage,
            data=blob,
            name=name,
            content_type=ctype,
            fmt=format,
            content_hash=content_hash,
            job_id=uuid.UUID(job_id) if job_id else None,
            kind=kind,
            title=title,
            template=template,
        )
        return service.view(doc, session.get(Render, doc.id), deduplicated=not created)

    def file_doc(
        session: Session,
        uri: str,
        title: str,
        kind: str = "document",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if not storage.exists(uri):
            raise LookupError(f"nothing stored at {uri}")
        blob = storage.get(uri)
        doc, created = service.add_version(
            session,
            storage,
            data=blob,
            name=title,
            content_type="application/octet-stream",
            fmt="file",
            content_hash=service.sha256(blob),
            job_id=uuid.UUID(job_id) if job_id else None,
            kind=kind,
            title=title,
        )
        return service.view(doc, session.get(Render, doc.id), deduplicated=not created)

    def request_signature(
        session: Session,
        document_id: str,
        signers: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        existing = session.scalar(
            select(Signature).where(Signature.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return _sig_view(existing, replayed=True)
        doc = session.get(Document, uuid.UUID(document_id))
        if doc is None:
            raise LookupError(f"document {document_id} not found")
        who = [Signer(**s) for s in signers]
        if not who:
            raise ValueError("at least one signer is required")
        extra: dict[str, Any] = {}
        if "idempotency_key" in inspect.signature(sign.request_signature).parameters:
            extra["idempotency_key"] = idempotency_key  # a new key is a new provider request
        ref = asyncio.run(sign.request_signature(doc.id, who, **extra))
        sig = Signature(
            document_id=doc.id,
            idempotency_key=idempotency_key,
            system=ref.system,
            request_id=ref.id,
            status="requested",
            signers=[s.model_dump() for s in who],
            signed_by=[],
        )
        session.add(sig)
        record(
            session,
            CAP,
            "documents.signature_requested",
            str(sig.id),
            document_id=doc.id,
            request_id=ref.id,
        )
        session.flush()
        return _sig_view(sig, url=ref.url)

    signer_schema = _obj({"name": _STR, "email": _STR, "role": _STR}, required=["name", "email"])
    kind_prop = {"type": "string", "maxLength": 60}
    for tool in (
        Tool(
            "documents.render",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "template": _STR,
                    "title": _STR,
                    "data": {"type": "object"},
                    "kind": kind_prop,
                    "job_id": {"type": "string", "format": "uuid"},
                    "format": {"type": "string", "enum": ["pdf", "html"]},
                },
                ["template", "title"],
            ),
            render_doc,
            "Render a template (industry slot or template-root path) with data to a versioned "
            "document; the same inputs return the same document",
        ),
        Tool(
            "documents.file",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "uri": _STR,
                    "title": _STR,
                    "kind": kind_prop,
                    "job_id": {"type": "string", "format": "uuid"},
                },
                ["uri", "title"],
            ),
            file_doc,
            "Attach an already stored file to a job as a document",
        ),
        Tool(
            "documents.request_signature",
            CAP,
            SideEffect.EXTERNAL,
            _obj(
                {
                    "document_id": {"type": "string", "format": "uuid"},
                    "signers": {"type": "array", "minItems": 1, "items": signer_schema},
                    "idempotency_key": _STR,
                },
                ["document_id", "signers", "idempotency_key"],
            ),
            request_signature,
            "Ask the signers to sign a document, once per idempotency key",
        ),
    ):
        registry.register(tool)
