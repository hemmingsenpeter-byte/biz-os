"""Auth settings read from the environment at call time."""

from __future__ import annotations

import os

SESSION_COOKIE = "biz_session"
SESSION_MAX_AGE = 60 * 60 * 12
MAGIC_LINK_MAX_AGE = 15 * 60
INVITE_MAX_AGE = 24 * 60 * 60
CHALLENGE_TTL = 5 * 60


def secret_key() -> str:
    key = os.environ.get("BIZ_SECRET_KEY")
    if not key:
        raise RuntimeError("BIZ_SECRET_KEY is not set")
    return key


def rp_id() -> str:
    return os.environ.get("BIZ_RP_ID", "localhost")


def origin() -> str:
    return os.environ.get("BIZ_ORIGIN", "http://localhost:3000")


def cookie_secure() -> bool:
    return origin().startswith("https://")
