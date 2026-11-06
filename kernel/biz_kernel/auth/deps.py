"""FastAPI dependencies: db session, current user, owner guard."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from biz_kernel import db
from biz_kernel.auth import config, tokens
from biz_kernel.auth.models import User


def get_session() -> Iterator[Session]:
    """One session per request; commits on success. Tests override this dependency."""
    with db.session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_session)]


def current_user(
    session: DbSession,
    biz_session: Annotated[str | None, Cookie(alias=config.SESSION_COOKIE)] = None,
) -> User:
    if not biz_session:
        raise HTTPException(401, "not signed in")
    user_id = tokens.read_session(biz_session)
    user = session.get(User, user_id) if user_id else None
    if user is None or not user.active:
        raise HTTPException(401, "not signed in")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def owner_only(user: CurrentUser) -> User:
    if user.role != "owner":
        raise HTTPException(403, "owner only")
    return user


OwnerUser = Annotated[User, Depends(owner_only)]
