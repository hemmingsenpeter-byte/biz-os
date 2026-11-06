"""Pure money maths and price-book logic. All money is Decimal, rounded half-up to cents."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from biz_cap_quoting.config import QuotingPolicy
from biz_cap_quoting.models import PriceItem

CENT = Decimal("0.01")
PCT = Decimal("0.01")
HUNDRED = Decimal(100)


def dec(value: Any) -> Decimal:
    """Parse JSON numbers/strings without float noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a number: {value!r}")
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def pct2(value: Decimal) -> Decimal:
    return value.quantize(PCT, rounding=ROUND_HALF_UP)


def minor(value: Decimal) -> int:
    return int((money(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def margin_pct(cost: Decimal, price: Decimal) -> Decimal:
    """Margin on price: (price - cost) / price, as a percentage."""
    if price <= 0:
        return Decimal("0.00")
    return pct2((price - cost) / price * HUNDRED)


@dataclass
class PricedLine:
    position: int
    code: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_cost: Decimal
    unit_price: Decimal
    line_cost: Decimal
    line_price: Decimal
    unpriced: bool
    cost_basis: dict[str, Any]


@dataclass
class Totals:
    total_cost: Decimal
    total_price: Decimal
    margin_pct: Decimal
    margin_ok: bool
    warnings: list[str] = field(default_factory=list)


def _non_negative(value: Decimal, what: str) -> Decimal:
    if value < 0:
        raise ValueError(f"{what} must not be negative (negative_value): {value}")
    return value


def price_lines(
    session: Session, raw: list[dict[str, Any]], *, allow_credit: bool = False
) -> tuple[list[PricedLine], list[str]]:
    """Resolve lines against the price book. A line needs a book code or `unpriced: true`.

    Costs on book lines always come from the price book: a caller-supplied `unit_cost` is
    never trusted. Prices may be overridden (with a warning); the margin check guards them.
    Negative costs and prices are refused; negative quantities only when `allow_credit`
    (removed work in a variation)."""
    out: list[PricedLine] = []
    warnings: list[str] = []
    for i, ln in enumerate(raw, start=1):
        code = ln.get("code") or None
        desc = str(ln.get("description") or (code or ""))
        qty = dec(ln["quantity"])
        if not allow_credit:
            _non_negative(qty, f"line {i}: quantity")
        unpriced = bool(ln.get("unpriced"))
        item = session.get(PriceItem, code) if code else None
        if code and item is None and not unpriced:
            raise ValueError(
                f"line {i}: code {code!r} is not in the price book (unknown_trade_item)"
            )
        if not code and not unpriced:
            raise ValueError(
                f"line {i}: needs a price-book code or unpriced=true (unknown_trade_item)"
            )
        given_cost = ln.get("unit_cost")
        given_price = ln.get("unit_price")
        if given_cost is not None:
            _non_negative(dec(given_cost), f"line {i}: unit_cost")
        if given_price is not None:
            _non_negative(dec(given_price), f"line {i}: unit_price")
        basis: dict[str, Any]
        if item is not None:
            cost = item.unit_cost
            if given_cost is not None and dec(given_cost) != item.unit_cost:
                warnings.append(
                    f"cost_override_ignored: line {i} ({code}) cost taken from the price book"
                )
            price = dec(given_price) if given_price is not None else item.unit_price
            overridden = price != item.unit_price
            if overridden:
                warnings.append(f"price_override: line {i} ({code}) differs from the price book")
            basis = {
                "source": "price_book_override" if overridden else "price_book",
                "code": code,
                "revision": item.revision,
                "book_unit_cost": str(item.unit_cost),
                "book_unit_price": str(item.unit_price),
            }
            unit = str(ln.get("unit") or item.unit)
        else:
            cost = dec(given_cost or 0)
            price = dec(given_price or 0)
            basis = {"source": "unpriced", "code": code}
            warnings.append(f"unpriced_line: line {i} ({desc}) has no price-book basis")
            unit = str(ln.get("unit") or "")
        out.append(
            PricedLine(
                position=i,
                code=code,
                description=desc,
                quantity=qty,
                unit=unit,
                unit_cost=cost,
                unit_price=price,
                line_cost=money(qty * cost),
                line_price=money(qty * price),
                unpriced=item is None,
                cost_basis=basis,
            )
        )
    return out, warnings


def totals(
    lines: list[PricedLine],
    policy: QuotingPolicy,
    contingency_pct: Decimal = Decimal(0),
    discount_pct: Decimal = Decimal(0),
) -> Totals:
    """Contingency raises cost and price alike (margin unchanged); discount cuts price only.

    Any unpriced line makes `margin_ok` false: its cost is unknown, so the margin is too."""
    _non_negative(contingency_pct, "contingency_pct")
    _non_negative(discount_pct, "discount_pct")
    if discount_pct >= HUNDRED:
        raise ValueError(f"discount_pct must be under 100 (negative_value): {discount_pct}")
    cost = sum((ln.line_cost for ln in lines), Decimal(0))
    price = sum((ln.line_price for ln in lines), Decimal(0))
    if contingency_pct:
        factor = Decimal(1) + contingency_pct / HUNDRED
        cost, price = money(cost * factor), money(price * factor)
    if discount_pct:
        price = money(price * (Decimal(1) - discount_pct / HUNDRED))
    m = margin_pct(cost, price)
    warnings: list[str] = []
    ok = True
    if price <= 0 and lines:
        ok = False
        warnings.append("zero_total: nothing priced")
    elif m < policy.min_margin_pct:
        ok = False
        warnings.append(f"margin_below_policy: {m}% is under the {policy.min_margin_pct}% minimum")
    elif m < policy.target_margin_pct:
        warnings.append(
            f"margin_below_target: {m}% is under the {policy.target_margin_pct}% target"
        )
    if discount_pct > policy.max_discount_pct:
        ok = False
        warnings.append(
            f"discount_above_policy: {discount_pct}% exceeds the {policy.max_discount_pct}% maximum"
        )
    n_unpriced = sum(1 for ln in lines if ln.unpriced)
    if n_unpriced:
        ok = False
        warnings.append(
            f"unpriced_blocks_margin: {n_unpriced} unpriced line(s); margin cannot be confirmed"
        )
    return Totals(cost, price, m, ok, warnings)


def item_dict(i: PriceItem) -> dict[str, Any]:
    return {
        "code": i.code,
        "category": i.category,
        "description": i.description,
        "unit": i.unit,
        "unit_cost": str(i.unit_cost),
        "unit_price": str(i.unit_price),
    }
