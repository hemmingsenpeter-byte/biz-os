"""cash: a 13-week cash forecast from the bank balance, receivables and payables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_cash import config
from biz_cap_cash.tools import forecast, register

__all__ = ["forecast", "pollers", "register"]


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds config (floor, time zone); asks for the weekly cash review."""
    from biz_cap_cash.review import floor_setting, poll_review

    config.bind(rt.manifest)
    floor_setting()  # a bad floor fails at start, not on the first review
    return [lambda: poll_review(rt)]
