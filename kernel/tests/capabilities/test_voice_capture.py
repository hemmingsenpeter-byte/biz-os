"""voice_capture tools and owner upload route."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from biz_cap_voice_capture import owner_routes, register
from biz_cap_voice_capture.models import VoiceNote
from biz_kernel.api.app import create_app
from biz_kernel.auth.deps import current_user, get_session
from biz_kernel.packets.bus import Bus, SchemaRegistry
from biz_kernel.records.models import Job, Note, Party, Site, Task
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeSpeech
from fastapi.testclient import TestClient
from sqlalchemy import func, select

ROOT = Path(__file__).parents[3]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    monkeypatch.setenv("BIZ_STORAGE_ROOT", str(tmp_path))
    speech = FakeSpeech("Pop round to Harbour Lane and fix two sockets")
    reg = ToolRegistry()
    register(reg, {"speech": speech})
    return SimpleNamespace(speech=speech, reg=reg, root=tmp_path)


def call(env: SimpleNamespace, session, name: str, **args):  # type: ignore[no-untyped-def]
    return env.reg.get(name).fn(session=session, **args)


def make_job(session, label: str, client: str, kind: str = "fitout") -> Job:  # type: ignore[no-untyped-def]
    party = Party(kind="organisation", name=client)
    site = Site(label=label)
    session.add_all([party, site])
    session.flush()
    job = Job(client_party_id=party.id, site_id=site.id, kind=kind, stage="active")
    session.add(job)
    session.flush()
    return job


def audio(env: SimpleNamespace, name: str = "a.wav", data: bytes = b"RIFFdata") -> Path:
    p = env.root / name
    p.write_bytes(data)
    return p


def test_transcribe_idempotent_on_sha(env, session) -> None:  # type: ignore[no-untyped-def]
    a, b = audio(env, "a.wav"), audio(env, "copy.wav")  # same bytes
    first = call(env, session, "voice.transcribe", audio_uri=a.as_uri())
    assert first["cached"] is False and "sockets" in first["transcript"]
    again = call(env, session, "voice.transcribe", audio_uri=str(b))
    assert again["cached"] is True and again["voice_note_id"] == first["voice_note_id"]
    assert len(env.speech.calls) == 1
    assert session.scalar(select(func.count()).select_from(VoiceNote)) == 1
    other = audio(env, "c.wav", b"different")
    assert call(env, session, "voice.transcribe", audio_uri=str(other))["cached"] is False
    assert len(env.speech.calls) == 2


def test_transcribe_refuses_bad_uris(env, session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for uri in ["http://x/a.wav", "../a.wav", "/etc/passwd", "file:///etc/passwd"]:
        with pytest.raises(ValueError):
            call(env, session, "voice.transcribe", audio_uri=uri)
    assert env.speech.calls == []


def test_file_note_writes_note_and_tasks(env, session) -> None:  # type: ignore[no-untyped-def]
    job, other = make_job(session, "Harbour Lane", "Acme"), make_job(session, "Elm", "Bolt")
    got = call(
        env,
        session,
        "voice.file_note",
        job_id=str(job.id),
        summary="Sockets to fix",
        issues=["damp wall"],
        action_items=[
            {"title": "Order sockets", "due": "2030-01-07", "assignee": "sam"},
            {"title": "Call client", "due": "Friday"},
        ],
    )
    note = session.get(Note, uuid.UUID(got["note_id"]))
    assert note and note.job_id == job.id and note.source == "voice"
    assert "damp wall" in note.body
    tasks = session.scalars(select(Task).where(Task.job_id == job.id).order_by(Task.title)).all()
    assert [t.title for t in tasks] == ["Call client (due: Friday)", "Order sockets"]
    assert tasks[1].due_at is not None and tasks[1].assignee == "sam"
    assert tasks[0].due_at is None
    assert not session.scalars(select(Task).where(Task.job_id == other.id)).all()


def test_file_note_unknown_job_and_voice_note_state(env, session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(LookupError):
        call(env, session, "voice.file_note", job_id=str(uuid.uuid4()), summary="x")
    assert session.scalar(select(func.count()).select_from(Note)) == 0
    job = make_job(session, "Harbour Lane", "Acme")
    t = call(env, session, "voice.transcribe", audio_uri=str(audio(env)))
    args = {"job_id": str(job.id), "summary": "s", "voice_note_id": t["voice_note_id"]}
    assert call(env, session, "voice.file_note", **args)["already_filed"] is False
    assert session.get(VoiceNote, uuid.UUID(t["voice_note_id"])).status == "filed"  # type: ignore[union-attr]
    assert call(env, session, "voice.file_note", **args)["already_filed"] is True
    assert session.scalar(select(func.count()).select_from(Note)) == 1


def test_match_job_ranking(env, session) -> None:  # type: ignore[no-untyped-def]
    a = make_job(session, "Harbour Lane", "Acme Ltd", "fitout")
    b = make_job(session, "Elm Court", "Acme Ltd", "repair")
    make_job(session, "Quarry Rd", "Zed Corp", "install")
    out = call(
        env,
        session,
        "voice.match_job",
        transcript="Been at Harbour Lane today for Acme, the fitout is going well",
    )
    ids = [c["job_id"] for c in out["candidates"]]
    assert ids == [str(a.id), str(b.id)]
    assert out["candidates"][0]["score"] > out["candidates"][1]["score"]
    assert out["ambiguous"] is False and out["confirm_required"] is True
    tie = call(env, session, "voice.match_job", transcript="Acme Ltd called")
    assert tie["ambiguous"] is True and len(tie["candidates"]) == 2
    none = call(env, session, "voice.match_job", transcript="nothing relevant here")
    assert none["candidates"] == [] and none["ambiguous"] is True


def test_owner_upload_route(env, session) -> None:  # type: ignore[no-untyped-def]
    schemas = SchemaRegistry()
    schemas.load_dir(ROOT / "packets")
    rt = SimpleNamespace(bus=Bus(schemas))
    app = create_app()
    app.include_router(owner_routes(rt), prefix="/v1")
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    files = {"file": ("note.m4a", b"audio-bytes", "audio/mp4")}
    assert client.post("/v1/voice_capture/upload", files=files).status_code == 401
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(email="o@x.org")
    r = client.post("/v1/voice_capture/upload", files=files)
    assert r.status_code == 201 and r.json()["duplicate"] is False
    assert Path(r.json()["audio_uri"].removeprefix("file://")).is_file()
    again = client.post("/v1/voice_capture/upload", files=files)
    assert again.json()["duplicate"] is True
    assert again.json()["voice_note_id"] == r.json()["voice_note_id"]
    t = call(env, session, "voice.transcribe", audio_uri=r.json()["audio_uri"])
    assert t["voice_note_id"] == r.json()["voice_note_id"]
