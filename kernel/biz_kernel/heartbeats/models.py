"""Wake-up queue. At most one pending wake-up per role (partial unique index)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from biz_kernel.db import Base, uuid7


class Wakeup(Base):
    __tablename__ = "wakeups"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    role: Mapped[str] = mapped_column(String(120))
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text))
    due_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|claimed|done
    coalesced_count: Mapped[int] = mapped_column(default=1)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_wakeups_pending_role",
            "role",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
