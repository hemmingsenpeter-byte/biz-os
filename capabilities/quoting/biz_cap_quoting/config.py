"""Composition-level quoting policy, bound from the runtime's manifest.

Policy shape (composition policy file):
    quoting: {min_margin_pct, target_margin_pct, max_discount_pct, currency}
`currency` falls back to a top-level policy `currency`, then to DEFAULT_CURRENCY.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

DEFAULT_CURRENCY = "NZD"
_manifest: Any = None


@dataclass(frozen=True)
class QuotingPolicy:
    min_margin_pct: Decimal = Decimal("10")
    target_margin_pct: Decimal = Decimal("20")
    max_discount_pct: Decimal = Decimal("10")
    currency: str = DEFAULT_CURRENCY


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def policy() -> QuotingPolicy:
    """Read at call time from the bound manifest; defaults apply when unbound or unset."""
    pol = (getattr(_manifest, "policy", None) or {}) if _manifest is not None else {}
    own = pol.get("quoting") or {}
    base = QuotingPolicy()

    def num(name: str, default: Decimal) -> Decimal:
        return Decimal(str(own[name])) if own.get(name) is not None else default

    return QuotingPolicy(
        min_margin_pct=num("min_margin_pct", base.min_margin_pct),
        target_margin_pct=num("target_margin_pct", base.target_margin_pct),
        max_discount_pct=num("max_discount_pct", base.max_discount_pct),
        currency=str(own.get("currency") or pol.get("currency") or DEFAULT_CURRENCY),
    )
