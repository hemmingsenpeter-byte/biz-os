"""quoting: briefs, surveys, scopes, price book, estimates with margin policy, proposals."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_quoting import config
from biz_cap_quoting.models import (
    Brief,
    Estimate,
    EstimateLine,
    PriceHistory,
    PriceItem,
    Proposal,
    Scope,
    Survey,
    Variation,
)
from biz_cap_quoting.tools import register

__all__ = [
    "Brief",
    "Estimate",
    "EstimateLine",
    "PriceHistory",
    "PriceItem",
    "Proposal",
    "Scope",
    "Survey",
    "Variation",
    "pollers",
    "register",
]


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: called once when the runtime is built; binds the composition's quoting policy."""
    config.bind(rt.manifest)
    return []
