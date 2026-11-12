"""User administration helpers (used by the CLI and tests)."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from biz_kernel.auth.models import User


def create_user(session: Session, email: str, role: str, display_name: str) -> User:
    if role not in ("owner", "staff"):
        raise ValueError(f"unknown role {role!r}")
    user = User(email=email.strip().lower(), role=role, display_name=display_name)
    session.add(user)
    session.flush()
    return user


def prepare_owner_invite(
    session: Session, email: str, display_name: str, *, reissue: bool = False
) -> User:
    """Create the first owner, or explicitly recover enrolment for an active owner.

    The caller must commit this transaction before exposing its invite token.
    Serializing the check prevents concurrent local bootstrap commands from each
    creating a different first owner. Existing users are never promoted or renamed.
    """
    email = email.strip().lower()
    display_name = display_name.strip()
    if (
        not email
        or len(email) > 320
        or email.count("@") != 1
        or any(char.isspace() for char in email)
        or not all(email.split("@"))
    ):
        raise ValueError("provide an email address with one @ and no whitespace")
    if not display_name or len(display_name) > 200:
        raise ValueError("name must contain between 1 and 200 characters")
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "biz:first-owner-invite"},
    )
    existing = session.scalar(select(User).where(User.email == email))
    if reissue:
        if existing is None or existing.role != "owner" or not existing.active:
            raise ValueError("--reissue requires the email of an existing active owner")
        return existing
    if session.scalar(select(User.id).where(User.role == "owner").limit(1)) is not None:
        raise ValueError("an owner already exists; use --reissue for that owner's enrolment")
    if existing is not None:
        raise ValueError("this email already belongs to a user; existing users are never promoted")
    return create_user(session, email, "owner", display_name)
