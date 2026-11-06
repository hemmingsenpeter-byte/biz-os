"""voice_capture tools. The transcript is untrusted text: tools store and score it, never
interpret it. Structured extraction happens in an SOP draft step (router fences the text)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from biz_kernel.contracts.ports import SpeechPort
from biz_kernel.policy.types import SideEffect
from biz_kernel.records.models import Job, Note, Party, Site, Task
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_voice_capture.models import VoiceNote

CAP = "voice"
_STR: dict[str, Any] = {"type": "string"}
_STOP = {
    "the", "and", "that", "this", "with", "have", "from", "was", "were", "for", "are", "but",
    "not", "you", "all", "can", "will", "just", "about", "need", "then", "there", "they",
    "job", "site", "street", "road", "work", "done", "today", "tomorrow", "also", "going",
}  # fmt: skip


def storage_root() -> Path:
    return Path(os.environ.get("BIZ_STORAGE_ROOT", "./storage"))


def resolve_stored(audio_uri: str, root: Path | None = None) -> Path:
    """file:// URI or plain path inside the storage root; anything else is refused."""
    base = (root or storage_root()).resolve()
    parsed = urlparse(audio_uri)
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        raw = unquote(parsed.path)
    elif parsed.scheme == "":
        raw = audio_uri
    else:
        raise ValueError(f"audio uri {audio_uri!r} refused: only local files are accepted")
    if not raw or "\x00" in raw:
        raise ValueError("audio uri is empty or invalid")
    p = Path(raw)
    resolved = (p if p.is_absolute() else base / p).resolve()
    if base not in resolved.parents:
        raise ValueError("audio path is outside the storage root")
    return resolved


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3 and t not in _STOP}


def score_job(transcript_tokens: set[str], fields: list[tuple[str, int]]) -> tuple[int, list[str]]:
    """Sum weight per distinct field token mentioned; also returns the matched tokens."""
    total, hit = 0, set()
    for text, weight in fields:
        for t in tokens(text) & transcript_tokens:
            total += weight
            hit.add(t)
    return total, sorted(hit)


def _due(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    speech: SpeechPort = ports["speech"]

    def _out(row: VoiceNote, cached: bool) -> dict[str, Any]:
        return {
            "voice_note_id": str(row.id),
            "transcript": row.transcript or "",
            "language": row.language,
            "duration_s": row.duration,
            "status": row.status,
            "cached": cached,
        }

    def transcribe(
        session: Session,
        audio_uri: str,
        job_id: str | None = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        path = resolve_stored(audio_uri)
        if not path.is_file():
            raise FileNotFoundError(f"no such audio file: {audio_uri}")
        digest = sha256_file(path)
        row = session.scalar(select(VoiceNote).where(VoiceNote.sha256 == digest))
        if row is not None and row.transcript is not None and row.status != "received":
            return _out(row, True)  # the same audio never transcribes twice
        if row is None:
            row = VoiceNote(
                audio_uri=audio_uri,
                sha256=digest,
                job_id=uuid.UUID(job_id) if job_id else None,
                created_by=created_by,
            )
            session.add(row)
        t = asyncio.run(speech.transcribe(path.as_uri()))
        row.transcript = t.text
        row.language = t.language
        row.duration = t.duration_s
        row.status = "transcribed"
        session.flush()
        return _out(row, False)

    def match_job(session: Session, transcript: str, limit: int = 3) -> dict[str, Any]:
        want = tokens(transcript)
        rows = session.execute(
            select(Job, Site.label, Site.address, Party.name)
            .outerjoin(Site, Job.site_id == Site.id)
            .outerjoin(Party, Job.client_party_id == Party.id)
            .where(Job.closed_at.is_(None))
        ).all()
        scored = []
        for job, label, address, party in rows:
            s, hit = score_job(
                want,
                [(label or "", 3), (party or "", 2), (job.kind.replace("_", " "), 1),
                 (address or "", 1)],
            )  # fmt: skip
            if s > 0:
                scored.append(
                    {
                        "job_id": str(job.id),
                        "score": s,
                        "site": label,
                        "client": party,
                        "kind": job.kind,
                        "matched": hit,
                    }
                )
        scored.sort(key=lambda c: (-int(c["score"]), c["job_id"]))
        top = scored[: min(max(limit, 1), 3)]
        ambiguous = len(top) != 1 and not (
            len(top) > 1 and int(top[0]["score"]) > int(top[1]["score"])
        )
        return {"candidates": top, "ambiguous": ambiguous, "confirm_required": True}

    def file_note(
        session: Session,
        job_id: str,
        summary: str,
        voice_note_id: str | None = None,
        action_items: list[dict[str, Any]] | None = None,
        issues: list[str] | None = None,
        measurements: list[str] | None = None,
        author: str = "voice capture",
    ) -> dict[str, Any]:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise LookupError(f"unknown job {job_id}")
        vn = session.get(VoiceNote, uuid.UUID(voice_note_id)) if voice_note_id else None
        if voice_note_id and vn is None:
            raise LookupError(f"unknown voice note {voice_note_id}")
        if vn is not None and vn.status == "filed":
            return {
                "already_filed": True,
                "job_id": str(vn.job_id),
                "note_id": None,
                "task_ids": [],
            }
        body = summary.strip()
        if issues:
            body += "\n\nIssues:\n" + "\n".join(f"- {i}" for i in issues)
        if measurements:
            body += "\n\nMeasurements:\n" + "\n".join(f"- {m}" for m in measurements)
        note = Note(job_id=job.id, author=author, body=body, source="voice")
        session.add(note)
        task_ids = []
        for item in action_items or []:
            title = str(item["title"]).strip()
            due = item.get("due")
            parsed = _due(due)
            if due and parsed is None:
                title += f" (due: {due})"
            task = Task(
                job_id=job.id,
                title=title,
                due_at=parsed,
                assignee=item.get("assignee") or None,
            )
            session.add(task)
            task_ids.append(task)
        session.flush()
        if vn is not None:
            vn.job_id = job.id
            vn.status = "filed"
        session.flush()
        return {
            "already_filed": False,
            "job_id": str(job.id),
            "note_id": str(note.id),
            "task_ids": [str(t.id) for t in task_ids],
            "text": body,
        }

    def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": props,
        }

    registry.register(
        Tool(
            f"{CAP}.transcribe",
            CAP,
            SideEffect.DRAFT,
            obj({"audio_uri": _STR, "job_id": _STR, "created_by": _STR}, ["audio_uri"]),
            transcribe,
            "Transcribe a stored audio file once (idempotent on sha256)",
        )
    )
    registry.register(
        Tool(
            f"{CAP}.match_job",
            CAP,
            SideEffect.READ,
            obj({"transcript": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 3}},
                ["transcript"]),
            match_job,
            "Top candidate jobs for a transcript; never guesses silently",
        )
    )  # fmt: skip
    registry.register(
        Tool(
            f"{CAP}.file_note",
            CAP,
            SideEffect.DRAFT,
            obj(
                {
                    "job_id": _STR,
                    "summary": _STR,
                    "voice_note_id": _STR,
                    "action_items": {
                        "type": "array",
                        "items": obj(
                            {
                                "title": _STR,
                                "due": {"type": ["string", "null"]},
                                "assignee": {"type": ["string", "null"]},
                            },
                            ["title"],
                        ),
                    },
                    "issues": {"type": "array", "items": _STR},
                    "measurements": {"type": "array", "items": _STR},
                    "author": _STR,
                },
                ["job_id", "summary"],
            ),
            file_note,
            "File a voice Note and Tasks on a job",
        )
    )
