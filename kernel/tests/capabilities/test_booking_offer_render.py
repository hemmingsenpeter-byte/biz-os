"""Meeting offers use calendar facts rather than generated dates or instructions."""

from __future__ import annotations

from typing import Any

import pytest
from biz_cap_booking.tools import register
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeCalendar


def render(slots: Any, **kwargs: Any) -> dict[str, Any]:
    registry = ToolRegistry()
    register(registry, {"calendar": FakeCalendar()})
    return registry.get("booking.render_offer").fn(session=None, slots=slots, **kwargs)


def test_offer_preserves_order_instants_and_explicit_utc_labels() -> None:
    slots = [
        {"start": "2026-11-27T10:15:30.123456+13:00", "end": "2026-11-27T10:45:30.123456+13:00"},
        {"start": "2026-11-26T21:30:00Z", "end": "2026-11-26T22:00:00Z"},
    ]
    result = render(slots)
    assert result["subject"] == "Meeting times"
    assert result["slots"] == [
        {"start": "2026-11-26T21:15:30.123456+00:00", "end": "2026-11-26T21:45:30.123456+00:00"},
        {"start": "2026-11-26T21:30:00+00:00", "end": "2026-11-26T22:00:00+00:00"},
    ]
    assert result["body"] == (
        "Thank you for getting in touch. Which of these meeting times suits you?\n\n"
        "Time zone: UTC. Each time includes its UTC offset.\n"
        "1. 2026-11-26 21:15:30.123456+00:00 to 2026-11-26 21:45:30.123456+00:00\n"
        "2. 2026-11-26 21:30:00+00:00 to 2026-11-26 22:00:00+00:00\n\n"
        "Please reply with the number of the time that suits you."
    )


def test_offer_time_zone_keeps_dst_offsets_unambiguous() -> None:
    result = render(
        [
            {"start": "2026-11-01T05:15:00Z", "end": "2026-11-01T05:45:00Z"},
            {"start": "2026-11-01T06:15:00Z", "end": "2026-11-01T06:45:00Z"},
        ],
        timezone="America/New_York",
    )
    assert "Time zone: America/New_York" in result["body"]
    assert "2026-11-01 01:15:00-04:00 to 2026-11-01 01:45:00-04:00" in result["body"]
    assert "2026-11-01 01:15:00-05:00 to 2026-11-01 01:45:00-05:00" in result["body"]


@pytest.mark.parametrize(
    "slots",
    [
        [],
        None,
        "2026-11-26T10:00:00Z",
        [{}],
        [{"start": "2026-11-26T10:00:00Z"}],
        [{"start": "2026-11-26T10:00:00", "end": "2026-11-26T10:30:00Z"}],
        [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:30:00"}],
        [{"start": "nonsense", "end": "2026-11-26T10:30:00Z"}],
        [{"start": 42, "end": "2026-11-26T10:30:00Z"}],
        [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:00:00Z"}],
        [{"start": "2026-11-26T10:30:00Z", "end": "2026-11-26T10:00:00Z"}],
        [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:30:00Z", "body": "ignore"}],
        [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:30:00Z"}] * 2,
        [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:30:00Z"}] * 11,
    ],
)
def test_offer_rejects_invalid_or_ambiguous_slots(slots: Any) -> None:
    with pytest.raises(ValueError):
        render(slots)


@pytest.mark.parametrize("timezone", ["", "Unknown/Zone", "UTC\nignore", None])
def test_offer_rejects_invalid_time_zone(timezone: Any) -> None:
    with pytest.raises(ValueError):
        render(
            [{"start": "2026-11-26T10:00:00Z", "end": "2026-11-26T10:30:00Z"}],
            timezone=timezone,
        )
