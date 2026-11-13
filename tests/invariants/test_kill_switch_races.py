"""Kill switch races and ordering, from the review of PR #4. Each test failed on the
merged code; see docs/BUILD_LOG.md (2026-11-13)."""

from __future__ import annotations

import contextlib
import time
import types
from datetime import UTC, datetime, timedelta

import pytest
from biz_kernel import control, db
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged, suggestions
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth import tokens
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from biz_kernel.health.models import Alert
from biz_kernel.health.service import register_schedule, report_run
from biz_kernel.models_registry import import_all_models
from biz_kernel.policy.types import Action, SideEffect
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

T0 = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------- helpers


def _clock(monkeypatch: pytest.MonkeyPatch, ms: int, falling_random: bool = False) -> None:
    """Pin the clock biz_kernel.db.uuid7 reads (and, optionally, its random bits)."""
    monkeypatch.setattr(db, "time", types.SimpleNamespace(time_ns=lambda: ms * 1_000_000))
    if falling_random:
        counter = iter(range(2**70, 0, -(2**60)))
        monkeypatch.setattr(
            db,
            "secrets",
            types.SimpleNamespace(token_bytes=lambda n: next(counter).to_bytes(n, "big")),
        )


@pytest.fixture
def committed(fresh_db: str, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A real committed database behind db.session_scope(), restored afterwards."""
    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    monkeypatch.setattr(db, "_engine", db._engine)
    monkeypatch.setattr(db, "_factory", db._factory)
    configured = db.configure(fresh_db)
    yield
    configured.dispose()
    eng.dispose()


# ---------------------------------------------------------------- 1. suggestions


def test_suggestion_cannot_be_approved_while_paused(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """BUILD_LOG: while paused 'nothing can be approved'. The suggestions route still
    creates a staged operation already approved by the owner."""
    monkeypatch.setenv("BIZ_SECRET_KEY", "test-secret-not-real")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    owner = create_user(session, "review-owner@example.com", "owner", "O")
    sug = suggestions.propose(
        session, "planner", "Chase the late payer", "demo.send", {"to": "x"}, ["late"]
    )
    with TestClient(app) as c:
        c.cookies.set("biz_session", tokens.issue_session(owner.id))
        assert c.post("/v1/control/pause", json={"reason": "stop"}).status_code == 200
        r = c.post(f"/v1/suggestions/{sug.id}/approve")
    approved = session.scalars(
        select(StagedOperation).where(StagedOperation.status == "approved")
    ).all()
    assert r.status_code == 409, r.text
    assert approved == []


# ---------------------------------------------------------------- 2. pause ordering


def test_pause_from_a_host_whose_clock_is_behind_takes_effect(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`biz pause --url ...` from a machine 2 s behind the server: the newest event must
    win. The state is read as the row with the highest uuid7 id, which is client clock
    time, so the pause sorts before the earlier resume and the kill switch stays off."""
    now = time.time_ns() // 1_000_000
    _clock(monkeypatch, now)
    control.pause(session, "s@example.com", "first")
    _clock(monkeypatch, now + 1)
    assert control.resume(session, "owner").paused is False
    _clock(monkeypatch, now - 2_000)  # the CLI host
    state = control.pause(session, "cli", "stop everything")
    assert state.paused is True
    assert control.is_paused(session) is True


def test_pause_then_resume_in_the_same_millisecond(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two clicks (or two people) within one millisecond: uuid7 is not monotonic inside a
    millisecond (the low bits are random), so the later event can sort first."""
    _clock(monkeypatch, time.time_ns() // 1_000_000, falling_random=True)
    control.pause(session, "s@example.com", "odd")
    state = control.resume(session, "owner")
    assert state.paused is False
    assert control.is_paused(session) is False


# ---------------------------------------------------------------- 3. pause vs approve race


def test_a_pause_that_commits_before_the_approval_blocks_it(committed, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """gate.decide checks is_paused, then approves with nothing holding the pause state.
    A pause that commits in between does not stop the approval: the op ends up approved
    after the pause (audit order: pause, then approve)."""
    from biz_kernel.workflows import gate

    action = Action(
        actor="sales", tool="demo.send", side_effect=SideEffect.EXTERNAL, inputs={"b": 1}
    )
    with db.session_scope() as s:
        op_id = staged.stage(s, action, "owner").id

    real = staged.approve
    pause_committed: list[bool] = []

    def approve_after_a_pause(session, op_id, by, version):  # type: ignore[no-untyped-def]
        try:
            with db.session_scope() as other:  # a staff member presses Pause right now
                other.execute(text("SET LOCAL lock_timeout = '1s'"))
                control.pause(other, "s@example.com", "stop")
            pause_committed.append(True)
        except OperationalError:
            pass  # a fix that serialises pause and approve makes the pause wait
        return real(session, op_id, by, version)

    monkeypatch.setattr(staged, "approve", approve_after_a_pause)
    with contextlib.suppress(control.Paused):
        gate.decide(op_id, "owner", approve=True, version=1)
    with db.session_scope() as s:
        status = s.get(StagedOperation, op_id).status
    if pause_committed:  # the pause committed first, so the approval must not land
        assert status != "approved", "approved although the pause committed first"


# ---------------------------------------------------------------- 4. missed alerts


def test_resume_during_a_tick_raises_no_missed_schedule_alert(committed) -> None:  # type: ignore[no-untyped-def]
    """BUILD_LOG: 'Missed-schedule alerts are not raised while paused.' run_once reads the
    pause state again after the tick: a resume that lands while the paused tick runs
    turns every heartbeat the pause skipped into a schedule.missed alert."""
    from biz_kernel.workflows.worker import run_once

    with db.session_scope() as s:
        register_schedule(s, "heartbeat:sales", timedelta(minutes=1), T0)
        report_run(s, "heartbeat:sales", True, T0)
        control.pause(s, "owner", "looking into it")

    class Runtime:
        def tick(self, now: datetime) -> list[str]:
            with db.session_scope() as s:
                assert control.is_paused(s)  # the heartbeat itself was skipped
                control.resume(s, "owner")  # the owner resumes while the tick finishes
            return []

    run_once(Runtime(), T0 + timedelta(hours=2, seconds=30))
    with db.session_scope() as s:
        missed = s.scalars(select(Alert).where(Alert.kind == "schedule.missed")).all()
    assert missed == []
