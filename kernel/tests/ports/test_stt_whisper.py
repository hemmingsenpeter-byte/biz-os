"""stt_whisper adapter with an injected fake model."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from biz_kernel.contracts.suite import check_speech_port
from biz_port_stt_whisper import AudioRefused, WhisperSpeech, create


class FakeModel:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def transcribe(self, path: str):  # type: ignore[no-untyped-def]
        self.paths.append(path)
        segs = [
            SimpleNamespace(start=0.0, end=1.5, text=" Hello site "),
            SimpleNamespace(start=1.5, end=3.0, text=" two sockets "),
        ]
        return iter(segs), SimpleNamespace(language="en", duration=3.0)


async def test_contract_suite() -> None:
    await check_speech_port(WhisperSpeech(model=FakeModel(), root="/"))


async def test_transcribe_file_uri_and_plain(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"x")
    model = FakeModel()
    port = WhisperSpeech(model=model, root=tmp_path)
    t = await port.transcribe((tmp_path / "a.wav").as_uri())
    assert t.text == "Hello site two sockets"
    assert t.language == "en" and t.duration_s == 3.0
    assert t.segments[1] == {"start": 1.5, "end": 3.0, "text": "two sockets"}
    t2 = await port.transcribe("a.wav")  # relative to root
    assert t2.text == t.text
    assert model.paths[0] == model.paths[1]


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/a.wav",
        "https://example.com/a.wav",
        "ftp://example.com/a.wav",
        "file://evil.host/a.wav",
        "../outside.wav",
        "sub/../../outside.wav",
        "/etc/passwd",
        "file:///etc/passwd",
        "file:///root/../etc/passwd",
        "",
    ],
)
async def test_refuses(uri: str, tmp_path: Path) -> None:
    model = FakeModel()
    port = WhisperSpeech(model=model, root=tmp_path / "root")
    with pytest.raises(AudioRefused):
        await port.transcribe(uri)
    assert model.paths == []


def test_create_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BIZ_WHISPER_MODEL", "tiny")
    monkeypatch.setenv("BIZ_WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("BIZ_STORAGE_ROOT", str(tmp_path))
    port = create()
    assert (port.model_size, port.device, port.root) == ("tiny", "cpu", tmp_path)
