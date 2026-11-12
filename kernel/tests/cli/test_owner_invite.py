"""Local first-owner bootstrap and explicit passkey-enrolment recovery."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from urllib.parse import parse_qs, urlsplit

import pytest
from biz_kernel import migrations
from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth import tokens
from biz_kernel.auth.models import User
from biz_kernel.auth.users import create_user, prepare_owner_invite
from biz_kernel.cli.main import app
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()


@pytest.fixture
def owner_database(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> str:
    migrations.upgrade(fresh_db)
    monkeypatch.setenv("BIZ_DATABASE_URL", fresh_db)
    monkeypatch.setenv("BIZ_ORIGIN", "http://localhost:3100/")
    return fresh_db


def _invite(email: str = "owner@example.invalid", *extra: str):
    return runner.invoke(
        app,
        ["owner-invite", str(ROOT / "apps/demo"), "--email", email, "--json", *extra],
    )


def test_bootstrap_commits_owner_and_invite_is_single_use(owner_database: str) -> None:
    result = _invite(" Owner@Example.Invalid ", "--name", " Owner Name ")
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["email"] == "owner@example.invalid"
    assert output["expires_in_seconds"] == 24 * 60 * 60
    link = urlsplit(output["invite_url"])
    assert (link.scheme, link.netloc, link.path) == (
        "http",
        "localhost:3100",
        "/auth/register",
    )
    token = parse_qs(link.query)["invite"][0]
    engine = create_engine(owner_database)
    try:
        with Session(engine) as session, session.begin():
            user = session.get(User, uuid.UUID(output["user_id"]))
            assert user is not None and user.role == "owner" and user.display_name == "Owner Name"
            assert tokens.consume_invite(session, token) == user.id
            assert tokens.consume_invite(session, token) is None
            audit = session.scalar(select(AuditEntry).where(AuditEntry.kind == "auth"))
            assert audit is not None and audit.subject == "first_owner_invited"
            assert token not in json.dumps(audit.detail)
    finally:
        engine.dispose()


def test_existing_owner_requires_explicit_reissue_and_is_not_renamed(owner_database: str) -> None:
    first = _invite("owner@example.invalid", "--name", "Original")
    assert first.exit_code == 0, first.output
    original = json.loads(first.stdout)
    assert _invite().exit_code == 1
    assert _invite("another@example.invalid").exit_code == 1
    assert _invite("another@example.invalid", "--reissue").exit_code == 1
    recovered = _invite(" OWNER@EXAMPLE.INVALID ", "--reissue", "--name", "Changed")
    assert recovered.exit_code == 0, recovered.output
    new_invite = json.loads(recovered.stdout)
    assert new_invite["user_id"] == original["user_id"]
    assert new_invite["invite_url"] != original["invite_url"]
    engine = create_engine(owner_database)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(User)) == 1
            owner = session.get(User, uuid.UUID(original["user_id"]))
            assert owner is not None and owner.display_name == "Original" and owner.role == "owner"
            assert session.scalar(
                select(AuditEntry.subject).where(AuditEntry.subject == "owner_invite_reissued")
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("role,active", [("staff", True), ("owner", False)])
def test_reissue_never_promotes_or_reactivates_users(
    owner_database: str, role: str, active: bool
) -> None:
    engine = create_engine(owner_database)
    try:
        with Session(engine) as session, session.begin():
            user = create_user(session, "existing@example.invalid", role, "Existing")
            user.active = active
            user_id = user.id
        result = _invite("existing@example.invalid", "--reissue")
        assert result.exit_code == 1 and "existing active owner" in result.output
        assert _invite("existing@example.invalid").exit_code == 1
        with Session(engine) as session:
            user = session.get(User, user_id)
            assert user is not None and user.role == role and user.active is active
            assert user.display_name == "Existing"
    finally:
        engine.dispose()


def test_missing_signing_secret_creates_no_account(
    owner_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIZ_SECRET_KEY", raising=False)
    result = _invite()
    assert result.exit_code == 1 and "BIZ_SECRET_KEY is not set" in result.output
    engine = create_engine(owner_database)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(User)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("email", ["", "missing-at", "@example.invalid", "a@", "a@@b", "a b@c"])
def test_invalid_email_is_refused(session: Session, email: str) -> None:
    with pytest.raises(ValueError, match="email address"):
        prepare_owner_invite(session, email, "Owner")
    assert session.scalar(select(func.count()).select_from(User)) == 0


def test_parallel_bootstraps_create_one_first_owner(owner_database: str) -> None:
    backend_pid: Queue[int] = Queue()
    engine = create_engine(owner_database)

    def second_bootstrap() -> str:
        with Session(engine) as session, session.begin():
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            backend_pid.put(pid)
            try:
                prepare_owner_invite(session, "second@example.invalid", "Owner")
                return "created"
            except ValueError:
                return "refused"

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with Session(engine) as first, first.begin():
            prepare_owner_invite(first, "first@example.invalid", "Owner")
            # Keep the first row uncommitted: a second check must wait rather than
            # treating the invisible row as permission to create another owner.
            second = pool.submit(second_bootstrap)
            pid = backend_pid.get(timeout=10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert not second.done(), "second bootstrap did not wait for the first transaction"
                with engine.connect() as connection:
                    waiting = connection.scalar(
                        text("SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": pid},
                    )
                if waiting == "advisory":
                    break
                time.sleep(0.02)
            else:
                pytest.fail("second bootstrap did not wait on the shared advisory lock")
        assert second.result(timeout=10) == "refused"
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(User)) == 1
    finally:
        pool.shutdown(wait=True)
        engine.dispose()
