"""Weekly cash review: publish cash.review_due once per local week, from Monday on.

The week is the business's local week (`limits.day_timezone`). The packet carries the
week's Monday and the configured floor (`cash.floor`, or null for none); the role's SOP
gathers the balance, receivables and payables and runs the forecast.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from biz_kernel import db
from biz_kernel.packets.bus import Packet
from sqlalchemy import select

from biz_cap_cash import config


def floor_setting() -> str | None:
    raw = config.setting("floor", None)
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"cash.floor {raw!r} is not a number") from None
    if isinstance(raw, bool) or not value.is_finite():
        raise ValueError(f"cash.floor {raw!r} is not a number")
    return str(value)


def poll_review(rt: Any, now: datetime | None = None) -> int:
    local = (now or db.utcnow()).astimezone(ZoneInfo(config.day_timezone())).date()
    monday = local - timedelta(days=local.weekday())
    key = f"cash.review_due:{monday.isoformat()}"
    with db.session_scope() as s:
        if s.scalar(select(Packet.id).where(Packet.idempotency_key == key)) is not None:
            return 0
        rt.bus.publish(
            s,
            type="cash.review_due",
            from_part="cash",
            summary=f"week of {monday.isoformat()}",
            payload={"week_start": monday.isoformat(), "floor": floor_setting()},
            idempotency_key=key,
        )
    return 1
