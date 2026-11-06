"""Signed, expiring tokens: session cookies, magic links and passkey-enrolment invites."""

from __future__ import annotations

import uuid

from itsdangerous import BadData, URLSafeTimedSerializer

from biz_kernel.auth import config


def _ser(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.secret_key(), salt=f"biz-{purpose}")


def issue(purpose: str, user_id: uuid.UUID) -> str:
    return _ser(purpose).dumps(str(user_id))


def read(purpose: str, token: str, max_age: int) -> uuid.UUID | None:
    """User id from a valid, unexpired token of this purpose; otherwise None."""
    try:
        return uuid.UUID(_ser(purpose).loads(token, max_age=max_age))
    except (BadData, ValueError):
        return None


def issue_session(user_id: uuid.UUID) -> str:
    return issue("session", user_id)


def read_session(token: str) -> uuid.UUID | None:
    return read("session", token, config.SESSION_MAX_AGE)


def issue_magic(user_id: uuid.UUID) -> str:
    return issue("magic", user_id)


def read_magic(token: str) -> uuid.UUID | None:
    return read("magic", token, config.MAGIC_LINK_MAX_AGE)


def issue_invite(user_id: uuid.UUID) -> str:
    """Lets a new user enrol their first passkey. Created by an admin or the CLI."""
    return issue("invite", user_id)


def read_invite(token: str) -> uuid.UUID | None:
    return read("invite", token, config.INVITE_MAX_AGE)
