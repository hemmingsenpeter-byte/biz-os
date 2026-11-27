"""Canonical meeting offers derived only from validated calendar slots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def render_offer(slots: list[dict[str, str]], timezone: str = "UTC") -> dict[str, Any]:
    """Keep slot order and exact instants; every displayed time has an explicit offset."""
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("timezone must name an IANA time zone")
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must name an IANA time zone") from exc
    if not isinstance(slots, list) or not 1 <= len(slots) <= 10:
        raise ValueError("offer needs between one and ten calendar slots")
    normalized = []
    labels = []
    seen = set()
    for index, slot in enumerate(slots, 1):
        if not isinstance(slot, dict) or set(slot) != {"start", "end"}:
            raise ValueError("each slot must contain only start and end")
        try:
            start, end = (datetime.fromisoformat(slot[key]) for key in ("start", "end"))
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("calendar slots must have explicit UTC offsets")
            start, end = start.astimezone(UTC), end.astimezone(UTC)
            if end <= start:
                raise ValueError("slot end must be after its start")
            if start in seen:
                raise ValueError("offered slot starts must be distinct")
            seen.add(start)
            first, last = (value.astimezone(zone).isoformat(sep=" ") for value in (start, end))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid or ambiguous calendar slot") from exc
        normalized.append({"start": start.isoformat(), "end": end.isoformat()})
        labels.append(f"{index}. {first} to {last}")
    return {
        "subject": "Meeting times",
        "body": (
            "Thank you for getting in touch. Which of these meeting times suits you?\n\n"
            f"Time zone: {timezone}. Each time includes its UTC offset.\n"
            + "\n".join(labels)
            + "\n\nPlease reply with the number of the time that suits you."
        ),
        "slots": normalized,
        "timezone": timezone,
    }
