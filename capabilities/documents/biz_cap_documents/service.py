"""Document versioning shared by render, file and signed-copy storage."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.contracts.ports import StoragePort
from biz_kernel.records.models import Document
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_documents.models import Render


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def latest(session: Session, job_id: uuid.UUID | None, kind: str, title: str) -> Document | None:
    stmt = select(Document).where(Document.kind == kind, Document.title == title)
    stmt = stmt.where(Document.job_id == job_id if job_id else Document.job_id.is_(None))
    return session.scalars(stmt.order_by(Document.version.desc()).limit(1)).first()


def view(doc: Document, render: Render | None, **extra: Any) -> dict[str, Any]:
    return {
        "document_id": str(doc.id),
        "version": doc.version,
        "uri": doc.uri,
        "sha256": doc.sha256,
        "supersedes_id": str(doc.supersedes_id) if doc.supersedes_id else None,
        "format": render.format if render else None,
        **extra,
    }


def add_version(
    session: Session,
    storage: StoragePort,
    *,
    data: bytes,
    name: str,
    content_type: str,
    fmt: str,
    content_hash: str,
    job_id: uuid.UUID | None,
    kind: str,
    title: str,
    template: str | None = None,
) -> tuple[Document, bool]:
    """Store `data` as the next version of (job, kind, title); same content hash -> no new row."""
    prev = latest(session, job_id, kind, title)
    if prev is not None:
        prev_render = session.get(Render, prev.id)
        if prev_render is not None and prev_render.content_hash == content_hash:
            return prev, False
    stored = storage.put(data, name, content_type)
    doc = Document(
        job_id=job_id,
        kind=kind,
        title=title,
        uri=stored.uri,
        sha256=stored.sha256,
        version=prev.version + 1 if prev else 1,
        supersedes_id=prev.id if prev else None,
    )
    session.add(doc)
    session.flush()
    session.add(
        Render(document_id=doc.id, content_hash=content_hash, template=template, format=fmt)
    )
    record(
        session,
        "documents",
        "documents.stored",
        str(doc.id),
        version=doc.version,
        sha256=doc.sha256,
        format=fmt,
    )
    session.flush()
    return doc, True
