"""Composition-level config for invoicing (`invoicing:` in policy.yaml), bound at runtime."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_manifest: Any = None


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def setting(name: str, default: Any) -> Any:
    if _manifest is None:
        return default
    return ((_manifest.policy or {}).get("invoicing") or {}).get(name, default)


def tax_rate() -> Decimal:
    """The business's sales-tax rate in percent (`tax.rate_pct` in policy.yaml, default 0).

    Stored amounts are tax-exclusive. The accounting system adds tax to what we post, so
    what is owed is the amount plus this tax.
    """
    raw = 0 if _manifest is None else ((_manifest.policy or {}).get("tax") or {}).get("rate_pct", 0)
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        raise ValueError(f"tax.rate_pct {raw!r} is not a number")
    rate = Decimal(str(raw))
    if not rate.is_finite() or not 0 <= rate <= 100:
        raise ValueError(f"tax.rate_pct {raw!r} must be from 0 to 100")
    return rate


def tax_on(amount: Decimal) -> Decimal:
    return (amount * tax_rate() / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
