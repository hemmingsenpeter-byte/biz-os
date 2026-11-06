"""Booking tools. offer_slots reads; book is external and idempotent. The SOP publishes
meeting.booked, never the tool."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from biz_kernel.contracts.ports import CalendarEvent, CalendarPort
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biz_cap_booking.models import Meeting

CAP = "booking"
_STR: dict[str, Any] = {"type": "string"}


class SlotUnavailable(RuntimeError):
    """The requested window is in the past or no longer free."""


def _aware(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def spread(slots: list[Any], limit: int) -> list[Any]:
    """Up to `limit` slots, preferring different days so the offer has real choice."""
    picked: list[Any] = []
    days: set[Any] = set()
    for s in slots:
        if s.start.date() not in days and len(picked) < limit:
            days.add(s.start.date())
            picked.append(s)
    for s in slots:
        if len(picked) >= limit:
            break
        if s not in picked:
            picked.append(s)
    return sorted(picked, key=lambda s: s.start)


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    calendar: CalendarPort = ports["calendar"]

    def offer_slots(
        session: Session,
        days_ahead: int = 7,
        duration_minutes: int = 30,
        limit: int = 3,
        not_before: str | None = None,
    ) -> dict[str, Any]:
        start = _aware(not_before) if not_before else datetime.now(UTC) + timedelta(hours=2)
        slots = asyncio.run(
            calendar.free_slots(start, start + timedelta(days=days_ahead), duration_minutes)
        )
        chosen = spread(slots, limit)
        if not chosen:
            raise LookupError("no free slots in the window")
        return {
            "slots": [{"start": s.start.isoformat(), "end": s.end.isoformat()} for s in chosen],
            "count": len(chosen),
        }

    def book(
        session: Session,
        start: str,
        end: str,
        title: str,
        idempotency_key: str,
        attendees: list[str] | None = None,
        location: str | None = None,
        notes: str | None = None,
        party_id: str | None = None,
    ) -> dict[str, Any]:
        prior = session.scalar(select(Meeting).where(Meeting.key == idempotency_key))
        if prior is not None:
            return {
                "meeting_id": str(prior.id),
                "starts_at": prior.starts_at.isoformat(),
                "external_id": prior.external_id,
            }
        event = CalendarEvent(
            title=title,
            start=_aware(start),
            end=_aware(end),
            attendees=attendees or [],
            location=location,
            body=notes,
        )
        if event.end <= event.start:
            raise SlotUnavailable("end must be after start")
        if event.start <= datetime.now(UTC):
            raise SlotUnavailable(f"{event.start.isoformat()} is in the past")
        minutes = int((event.end - event.start).total_seconds() // 60)
        # Re-check at book time: the offer may be stale, or another booking took the slot.
        free = asyncio.run(calendar.free_slots(event.start, event.end, minutes))
        if not any(sl.start == event.start for sl in free):
            raise SlotUnavailable(f"{event.start.isoformat()} is no longer free")
        meeting = Meeting(
            key=idempotency_key,
            party_id=uuid.UUID(party_id) if party_id else None,
            title=title,
            starts_at=event.start,
            ends_at=event.end,
        )
        try:
            with session.begin_nested():  # the unique index on active starts settles races
                session.add(meeting)
                session.flush()
        except IntegrityError as exc:
            raise SlotUnavailable(f"{event.start.isoformat()} is already booked") from exc
        ref = asyncio.run(calendar.book(event, idempotency_key))
        meeting.external_id = ref.id
        session.flush()
        return {
            "meeting_id": str(meeting.id),
            "starts_at": event.start.isoformat(),
            "external_id": ref.id,
        }

    def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": props,
        }

    registry.register(
        Tool(
            f"{CAP}.offer_slots",
            CAP,
            SideEffect.READ,
            obj(
                {
                    "days_ahead": {"type": "integer", "minimum": 1, "maximum": 60},
                    "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 480},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    "not_before": _STR,
                },
                [],
            ),
            offer_slots,
            "Offer free calendar slots",
        )
    )
    registry.register(
        Tool(
            f"{CAP}.book",
            CAP,
            SideEffect.EXTERNAL,
            obj(
                {
                    "start": _STR,
                    "end": _STR,
                    "title": _STR,
                    "idempotency_key": _STR,
                    "attendees": {"type": "array", "items": _STR},
                    "location": _STR,
                    "notes": _STR,
                    "party_id": _STR,
                },
                ["start", "end", "title", "idempotency_key"],
            ),
            book,
            "Book a calendar event once per idempotency key",
        )
    )
