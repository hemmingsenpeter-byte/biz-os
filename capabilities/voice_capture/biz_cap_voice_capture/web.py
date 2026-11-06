"""Owner-only upload. Mounted under /v1 by the composition (never the public /webhooks)."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from biz_kernel.auth.deps import DbSession, OwnerUser
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from biz_cap_voice_capture.models import VoiceNote
from biz_cap_voice_capture.tools import storage_root

MAX_BYTES = 100 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # form fields and boundaries around the file


def _stream_to_temp(file: UploadFile, directory: Path) -> tuple[Path, str, int]:
    """Copy the upload to a temp file with a running count; abort over MAX_BYTES."""
    directory.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    total = 0
    fd, name = tempfile.mkstemp(dir=directory, prefix=".upload-")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, "too large")
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, h.hexdigest(), total


class SizeLimitedRoute(APIRoute):
    """Checks Content-Length before FastAPI reads (spools) the multipart body."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        inner = super().get_route_handler()

        async def handler(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared is None or not declared.isdigit():
                raise HTTPException(411, "Content-Length required")
            if int(declared) > MAX_BYTES + MULTIPART_OVERHEAD:
                raise HTTPException(413, "too large")
            return await inner(request)

        return handler


def build_owner_router(rt: Any) -> APIRouter:
    router = APIRouter(
        prefix="/voice_capture", tags=["voice_capture"], route_class=SizeLimitedRoute
    )

    @router.post("/upload", status_code=201)
    async def upload(
        user: OwnerUser,
        session: DbSession,
        file: UploadFile = File(...),  # noqa: B008
        job_id: str | None = Form(None),
    ) -> dict[str, Any]:
        jid = None
        if job_id:
            try:
                jid = uuid.UUID(job_id)
            except ValueError as exc:
                raise HTTPException(422, "bad job_id") from exc
        voice_dir = storage_root() / "voice"
        tmp, digest, size = await run_in_threadpool(_stream_to_temp, file, voice_dir)
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "empty or too large")
        existing = session.scalar(select(VoiceNote).where(VoiceNote.sha256 == digest))
        duplicate = existing is not None
        if existing is None:
            ext = re.sub(r"[^a-z0-9]", "", Path(file.filename or "").suffix.lower())[:6]
            dest = voice_dir / f"{digest}{'.' + ext if ext else ''}"
            await run_in_threadpool(
                os.replace, tmp, dest
            )  # name is the hash: never user-controlled
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
        else:
            tmp.unlink(missing_ok=True)
        return {
            "voice_note_id": str(existing.id),
            "audio_uri": existing.audio_uri,
            "sha256": digest,
            "duplicate": duplicate,
        }

    return router
