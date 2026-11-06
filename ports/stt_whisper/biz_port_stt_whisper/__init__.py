"""faster-whisper adapter for SpeechPort. faster-whisper is an optional extra (`whisper`),
imported lazily so the package loads without it. Only local files under an allowed root
are transcribed: http(s) and path traversal are refused."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from biz_kernel.contracts.ports import Transcript

__all__ = ["AudioRefused", "WhisperSpeech", "create", "resolve_audio_path"]


class AudioRefused(ValueError):
    """The audio uri is not a local file inside the allowed root."""


def resolve_audio_path(audio_uri: str, root: Path) -> Path:
    """file:// URI or plain path -> resolved path strictly inside `root`."""
    parsed = urlparse(audio_uri)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise AudioRefused("remote file uri refused")
        raw = unquote(parsed.path)
    elif parsed.scheme == "" or (len(parsed.scheme) == 1 and audio_uri[1:2] == ":"):
        raw = audio_uri
    else:
        raise AudioRefused(f"scheme {parsed.scheme!r} refused; only file:// and plain paths")
    if not raw or "\x00" in raw:
        raise AudioRefused("empty or invalid path")
    base = root.resolve()
    path = Path(raw)
    resolved = (path if path.is_absolute() else base / path).resolve()
    if resolved != base and base not in resolved.parents:
        raise AudioRefused("path is outside the allowed root")
    return resolved


class WhisperSpeech:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: str = "int8",
        model: Any = None,
        root: str | Path | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.root = Path(root or os.environ.get("BIZ_STORAGE_ROOT", "./storage"))
        self._model = model

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError(
                    "faster-whisper is not installed; install the `whisper` extra"
                ) from exc
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def _run(self, path: Path) -> Transcript:
        segments, info = self._load().transcribe(str(path))
        segs: list[dict[str, Any]] = [
            {"start": float(s.start), "end": float(s.end), "text": str(s.text).strip()}
            for s in segments
        ]
        return Transcript(
            text=" ".join(str(s["text"]) for s in segs).strip(),
            language=getattr(info, "language", None),
            duration_s=getattr(info, "duration", None),
            segments=segs,
        )

    async def transcribe(self, audio_uri: str) -> Transcript:
        path = resolve_audio_path(audio_uri, self.root)
        return await asyncio.to_thread(self._run, path)


def create() -> WhisperSpeech:
    return WhisperSpeech(
        model_size=os.environ.get("BIZ_WHISPER_MODEL", "small"),
        device=os.environ.get("BIZ_WHISPER_DEVICE", "auto"),
        root=os.environ.get("BIZ_STORAGE_ROOT", "./storage"),
    )
