"""/v1/auth/* endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.auth import config, passkeys, tokens
from biz_kernel.auth.deps import CurrentUser, DbSession
from biz_kernel.auth.models import User
from biz_kernel.contracts.ports import OutboundMessage

router = APIRouter(prefix="/auth", tags=["auth"])


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    display_name: str

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(id=user.id, email=user.email, role=user.role, display_name=user.display_name)


def _sign_in(response: Response, session: Session, user: User, method: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        tokens.issue_session(user.id),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure(),
    )
    record(session, f"user:{user.id}", "auth", "sign_in", method=method)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.of(user)


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE)


class MagicRequest(BaseModel):
    email: str


@router.post("/magic/request", status_code=202)
async def magic_request(body: MagicRequest, request: Request, session: DbSession) -> dict[str, str]:
    """Always 202, so the response never reveals whether an address is a staff user."""
    mail = getattr(request.app.state, "mail", None)
    if mail is None:
        raise HTTPException(503, "mail is not configured")
    user = session.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is not None and user.active and user.role == "staff":
        token = tokens.issue_magic(user.id)
        link = f"{config.origin()}/auth/magic?token={token}"
        msg = OutboundMessage(
            to=[user.email],
            subject="Your sign-in link",
            body=f"Sign in: {link}\n\nThis link expires in 15 minutes.",
        )
        await mail.send(msg, idempotency_key=f"magic:{token}")
        record(session, f"user:{user.id}", "auth", "magic_link_sent")
    return {"status": "accepted"}


class MagicVerify(BaseModel):
    token: str


@router.post("/magic/verify", response_model=UserOut)
def magic_verify(body: MagicVerify, response: Response, session: DbSession) -> UserOut:
    user_id = tokens.consume_magic(session, body.token)
    user = session.get(User, user_id) if user_id else None
    if user is None or not user.active or user.role != "staff":
        raise HTTPException(401, "invalid or expired link")
    _sign_in(response, session, user, "magic_link")
    return UserOut.of(user)


class RegisterBegin(BaseModel):
    invite: str | None = None


@router.post("/passkey/register/begin")
def register_begin(
    body: RegisterBegin,
    request: Request,
    session: DbSession,
) -> dict[str, Any]:
    """Signed-in users add a passkey; new users present an invite token."""
    user: User | None = None
    if body.invite:
        uid = tokens.consume_invite(session, body.invite)
        user = session.get(User, uid) if uid else None
    else:
        cookie = request.cookies.get(config.SESSION_COOKIE)
        uid = tokens.read_session(cookie) if cookie else None
        user = session.get(User, uid) if uid else None
    if user is None or not user.active:
        raise HTTPException(401, "not allowed to register a passkey")
    return passkeys.begin_registration(session, user)


class RegisterFinish(BaseModel):
    challenge_id: uuid.UUID
    credential: dict[str, Any]
    label: str = ""


@router.post("/passkey/register/finish", status_code=201)
def register_finish(body: RegisterFinish, session: DbSession) -> dict[str, str]:
    try:
        cred = passkeys.finish_registration(session, body.challenge_id, body.credential, body.label)
    except passkeys.PasskeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    record(session, f"user:{cred.user_id}", "auth", "passkey_registered")
    return {"status": "registered"}


class AuthBegin(BaseModel):
    email: str | None = None


@router.post("/passkey/login/begin")
def login_begin(body: AuthBegin, session: DbSession) -> dict[str, Any]:
    return passkeys.begin_authentication(
        session, body.email.strip().lower() if body.email else None
    )


class AuthFinish(BaseModel):
    challenge_id: uuid.UUID
    credential: dict[str, Any]


@router.post("/passkey/login/finish", response_model=UserOut)
def login_finish(body: AuthFinish, response: Response, session: DbSession) -> UserOut:
    try:
        user = passkeys.finish_authentication(session, body.challenge_id, body.credential)
    except passkeys.PasskeyError as exc:
        raise HTTPException(401, str(exc)) from exc
    _sign_in(response, session, user, "passkey")
    return UserOut.of(user)
