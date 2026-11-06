"""voice_capture tables, prefixed cap_voice_capture_. Transcripts are untrusted text."""

from __future__ import annotations

import uuid
from datetime import datetime

from biz_kernel.db import Base, utcnow, uuid7
from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class VoiceNote(Base):
    __tablename__ = "cap_voice_capture_notes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID | None]
    audio_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    transcript: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="received")
    # received | transcribed | extracted | filed
    language: Mapped[str | None] = mapped_column(String(20))
    duration: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
