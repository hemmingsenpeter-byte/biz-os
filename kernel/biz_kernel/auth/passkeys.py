"""WebAuthn registration and authentication using the `webauthn` library."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from biz_kernel.auth import config
from biz_kernel.auth.models import AuthChallenge, PasskeyCredential, User
from biz_kernel.db import utcnow


class PasskeyError(ValueError):
    pass


def _store(session: Session, kind: str, user_id: uuid.UUID | None, challenge: bytes) -> uuid.UUID:
    row = AuthChallenge(
        kind=kind,
        user_id=user_id,
        challenge=challenge,
        expires_at=utcnow() + timedelta(seconds=config.CHALLENGE_TTL),
    )
    session.add(row)
    session.flush()
    return row.id


def _consume(session: Session, challenge_id: uuid.UUID, kind: str) -> AuthChallenge:
    row = session.get(AuthChallenge, challenge_id)
    if row is None or row.kind != kind:
        raise PasskeyError("unknown challenge")
    session.delete(row)
    session.flush()
    if row.expires_at < utcnow():
        raise PasskeyError("challenge expired")
    return row


def _descriptors(session: Session, user_id: uuid.UUID) -> list[PublicKeyCredentialDescriptor]:
    rows = session.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user_id)
    ).all()
    return [PublicKeyCredentialDescriptor(id=r.credential_id) for r in rows]


def begin_registration(session: Session, user: User) -> dict[str, Any]:
    options = generate_registration_options(
        rp_id=config.rp_id(),
        rp_name="BIZ OS",
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.display_name,
        exclude_credentials=_descriptors(session, user.id),
    )
    cid = _store(session, "register", user.id, options.challenge)
    return {"challenge_id": str(cid), "options": json.loads(options_to_json(options))}


def finish_registration(
    session: Session, challenge_id: uuid.UUID, credential: dict[str, Any], label: str = ""
) -> PasskeyCredential:
    row = _consume(session, challenge_id, "register")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=row.challenge,
            expected_rp_id=config.rp_id(),
            expected_origin=config.origin(),
        )
    except Exception as exc:
        raise PasskeyError(f"registration rejected: {exc}") from exc
    assert row.user_id is not None
    cred = PasskeyCredential(
        user_id=row.user_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        label=label,
    )
    session.add(cred)
    session.flush()
    return cred


def begin_authentication(session: Session, email: str | None = None) -> dict[str, Any]:
    """With an email, limit to that user's passkeys; without, allow any discoverable one."""
    user = session.scalar(select(User).where(User.email == email)) if email else None
    allow = _descriptors(session, user.id) if user else None
    options = generate_authentication_options(rp_id=config.rp_id(), allow_credentials=allow)
    cid = _store(session, "authenticate", user.id if user else None, options.challenge)
    return {"challenge_id": str(cid), "options": json.loads(options_to_json(options))}


def finish_authentication(
    session: Session, challenge_id: uuid.UUID, credential: dict[str, Any]
) -> User:
    row = _consume(session, challenge_id, "authenticate")
    try:
        raw_id = base64url_to_bytes(credential["rawId"])
    except Exception as exc:
        raise PasskeyError("malformed credential") from exc
    stored = session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == raw_id)
    )
    if stored is None or (row.user_id is not None and stored.user_id != row.user_id):
        raise PasskeyError("unknown credential")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=row.challenge,
            expected_rp_id=config.rp_id(),
            expected_origin=config.origin(),
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
        )
    except Exception as exc:
        raise PasskeyError(f"authentication rejected: {exc}") from exc
    stored.sign_count = verified.new_sign_count
    stored.last_used_at = utcnow()
    user = session.get(User, stored.user_id)
    if user is None or not user.active:
        raise PasskeyError("inactive user")
    session.flush()
    return user
