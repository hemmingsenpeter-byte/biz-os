"""Owner/staff users, their passkeys and short-lived WebAuthn challenges."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, utcnow, uuid7


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(10))  # owner | staff
    display_name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PasskeyCredential(Base):
    __tablename__ = "passkey_credentials"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(default=0)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]


class AuthChallenge(Base):
    """Server-side WebAuthn challenge; consumed once, expires quickly."""

    __tablename__ = "auth_challenges"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(20))  # register | authenticate
    user_id: Mapped[uuid.UUID | None]
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]
