"""Signed, expiring tokens: session cookies, magic links and passkey-enrolment invites."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biz_kernel.auth import config


def _ser(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.secret_key(), salt=f"biz-{purpose}")


def issue(purpose: str, user_id: uuid.UUID) -> str:
    # The nonce makes every token distinct, so single-use tokens can be burned one by one.
    return _ser(purpose).dumps({"u": str(user_id), "n": secrets.token_hex(8)})


def read(purpose: str, token: str, max_age: int) -> uuid.UUID | None:
    """User id from a valid, unexpired token of this purpose; otherwise None."""
    try:
        data = _ser(purpose).loads(token, max_age=max_age)
        return uuid.UUID(data["u"] if isinstance(data, dict) else data)
    except (BadData, ValueError, KeyError, TypeError):
        return None


def consume(session: Session, purpose: str, token: str, max_age: int) -> uuid.UUID | None:
    """Like read(), but a token works only once."""
    from biz_kernel.auth.models import UsedToken

    user_id = read(purpose, token, max_age)
    if user_id is None:
        return None
    sha = hashlib.sha256(token.encode()).hexdigest()
    try:
        with session.begin_nested():
            session.add(UsedToken(token_sha256=sha, purpose=purpose))
    except IntegrityError:
        return None
    return user_id


def issue_session(user_id: uuid.UUID) -> str:
    return issue("session", user_id)


def read_session(token: str) -> uuid.UUID | None:
    return read("session", token, config.SESSION_MAX_AGE)


def issue_magic(user_id: uuid.UUID) -> str:
    return issue("magic", user_id)


def consume_magic(session: Session, token: str) -> uuid.UUID | None:
    return consume(session, "magic", token, config.MAGIC_LINK_MAX_AGE)


def issue_invite(user_id: uuid.UUID) -> str:
    """Lets a new user enrol their first passkey. Created by an admin or the CLI."""
    return issue("invite", user_id)


def consume_invite(session: Session, token: str) -> uuid.UUID | None:
    return consume(session, "invite", token, config.INVITE_MAX_AGE)
