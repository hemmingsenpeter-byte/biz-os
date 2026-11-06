import re
import time

from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth import tokens
from biz_kernel.auth.models import AuthChallenge
from biz_kernel.auth.users import create_user
from sqlalchemy import select


async def test_magic_link_and_session(client, session, mail):
    create_user(session, "staff@example.com", "staff", "Sam")
    r = client.post("/v1/auth/magic/request", json={"email": "Staff@Example.com"})
    assert r.status_code == 202
    assert len(mail.sent) == 1
    token = re.search(r"token=(\S+)", mail.sent[0].body).group(1)

    assert client.get("/v1/auth/me").status_code == 401
    r = client.post("/v1/auth/magic/verify", json={"token": token})
    assert r.status_code == 200 and r.json()["role"] == "staff"
    assert "biz_session" in r.cookies
    assert client.get("/v1/auth/me").json()["email"] == "staff@example.com"
    kinds = [
        e.subject for e in session.scalars(select(AuditEntry).where(AuditEntry.kind == "auth"))
    ]
    assert "sign_in" in kinds

    client.post("/v1/auth/logout")
    assert client.get("/v1/auth/me").status_code == 401


def test_magic_link_does_not_leak_or_serve_owner(client, session, mail):
    create_user(session, "boss@example.com", "owner", "Boss")
    for email in ("boss@example.com", "nobody@example.com"):
        assert client.post("/v1/auth/magic/request", json={"email": email}).status_code == 202
    assert mail.sent == []
    owner = create_user(session, "boss2@example.com", "owner", "B")
    forged = tokens.issue_magic(owner.id)
    assert client.post("/v1/auth/magic/verify", json={"token": forged}).status_code == 401


def test_bad_and_expired_tokens(client, session, monkeypatch):
    staff = create_user(session, "t@example.com", "staff", "T")
    assert client.post("/v1/auth/magic/verify", json={"token": "junk"}).status_code == 401
    token = tokens.issue_magic(staff.id)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 16 * 60)
    assert client.post("/v1/auth/magic/verify", json={"token": token}).status_code == 401
    # a session token is not a magic token
    monkeypatch.setattr(time, "time", real)
    assert (
        client.post(
            "/v1/auth/magic/verify", json={"token": tokens.issue_session(staff.id)}
        ).status_code
        == 401
    )


def test_tampered_cookie_rejected(client, session):
    staff = create_user(session, "u@example.com", "staff", "U")
    client.cookies.set("biz_session", tokens.issue_session(staff.id) + "x")
    assert client.get("/v1/auth/me").status_code == 401


def test_register_begin_options(client, session):
    owner = create_user(session, "o@example.com", "owner", "Olive")
    assert client.post("/v1/auth/passkey/register/begin", json={}).status_code == 401
    r = client.post(
        "/v1/auth/passkey/register/begin", json={"invite": tokens.issue_invite(owner.id)}
    )
    assert r.status_code == 200
    opts = r.json()["options"]
    assert opts["rp"]["id"] == "localhost" and opts["user"]["name"] == "o@example.com"
    assert session.get(AuthChallenge, __import__("uuid").UUID(r.json()["challenge_id"])) is not None


def test_register_finish_rejects_bad_response(client, session):
    owner = create_user(session, "o2@example.com", "owner", "Olive")
    begin = client.post(
        "/v1/auth/passkey/register/begin", json={"invite": tokens.issue_invite(owner.id)}
    ).json()
    r = client.post(
        "/v1/auth/passkey/register/finish",
        json={"challenge_id": begin["challenge_id"], "credential": {"id": "x", "rawId": "x"}},
    )
    assert r.status_code == 400


def test_login_begin_and_finish_rejects_bad_response(client, session):
    create_user(session, "o3@example.com", "owner", "Olive")
    begin = client.post("/v1/auth/passkey/login/begin", json={"email": "o3@example.com"}).json()
    assert begin["options"]["rpId"] == "localhost" and begin["options"]["challenge"]
    r = client.post(
        "/v1/auth/passkey/login/finish",
        json={"challenge_id": begin["challenge_id"], "credential": {"id": "AAAA", "rawId": "AAAA"}},
    )
    assert r.status_code == 401
    assert client.get("/v1/auth/me").status_code == 401


def test_magic_link_works_once(client, session, mail):
    """Review finding 7: magic links can't be replayed."""
    import re

    from biz_kernel.auth.users import create_user

    create_user(session, "once@example.com", "staff", "Once")
    client.post("/v1/auth/magic/request", json={"email": "once@example.com"})
    token = re.search(r"token=(\S+)", mail.sent[-1].body).group(1)
    assert client.post("/v1/auth/magic/verify", json={"token": token}).status_code == 200
    assert client.post("/v1/auth/magic/verify", json={"token": token}).status_code == 401


def test_invite_works_once(client, session):
    from biz_kernel.auth import tokens
    from biz_kernel.auth.users import create_user

    user = create_user(session, "new@example.com", "staff", "New")
    invite = tokens.issue_invite(user.id)
    first = client.post("/v1/auth/passkey/register/begin", json={"invite": invite})
    assert first.status_code == 200
    again = client.post("/v1/auth/passkey/register/begin", json={"invite": invite})
    assert again.status_code == 401
