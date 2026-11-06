"""User administration helpers (used by the CLI and tests)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from biz_kernel.auth.models import User


def create_user(session: Session, email: str, role: str, display_name: str) -> User:
    if role not in ("owner", "staff"):
        raise ValueError(f"unknown role {role!r}")
    user = User(email=email.strip().lower(), role=role, display_name=display_name)
    session.add(user)
    session.flush()
    return user
