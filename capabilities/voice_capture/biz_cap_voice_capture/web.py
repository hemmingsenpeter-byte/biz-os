"""Owner-only upload. Mounted under /v1 by the composition (never the public /webhooks)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from biz_kernel.auth.deps import CurrentUser, DbSession
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from biz_cap_voice_capture.models import VoiceNote
from biz_cap_voice_capture.tools import storage_root

MAX_BYTES = 100 * 1024 * 1024


def build_owner_router(rt: Any) -> APIRouter:
    router = APIRouter(prefix="/voice_capture", tags=["voice_capture"])

    @router.post("/upload", status_code=201)
    async def upload(
        user: CurrentUser,
        session: DbSession,
        file: UploadFile = File(...),  # noqa: B008
        job_id: str | None = Form(None),
    ) -> dict[str, Any]:
        import hashlib

        data = await file.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            raise HTTPException(413 if data else 400, "empty or too large")
        jid = None
        if job_id:
            try:
                jid = uuid.UUID(job_id)
            except ValueError as exc:
                raise HTTPException(422, "bad job_id") from exc
        digest = hashlib.sha256(data).hexdigest()
        existing = session.scalar(select(VoiceNote).where(VoiceNote.sha256 == digest))
        duplicate = existing is not None
        if existing is None:
            ext = re.sub(r"[^a-z0-9]", "", Path(file.filename or "").suffix.lower())[:6]
            dest = storage_root() / "voice" / f"{digest}{'.' + ext if ext else ''}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)  # name is the hash: never user-controlled
            existing = VoiceNote(
                audio_uri=dest.resolve().as_uri(),
                sha256=digest,
                job_id=jid,
                created_by=str(user.email),
            )
            session.add(existing)
            session.flush()
            rt.bus.publish(
                session,
                type="voice.received",
                payload={
                    "voice_note_id": str(existing.id),
                    "audio_uri": existing.audio_uri,
                    "sha256": digest,
                    "job_id": str(jid) if jid else None,
                    "duplicate": False,
                },
                from_part="voice_capture",
                job_id=jid,
                summary="voice note received",
                idempotency_key=f"voice.received:{digest}",
            )
        return {
            "voice_note_id": str(existing.id),
            "audio_uri": existing.audio_uri,
            "sha256": digest,
            "duplicate": duplicate,
        }

    return router
