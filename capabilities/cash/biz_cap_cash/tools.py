"""Cash tools: a 13-week forecast from what is owed to us and what we owe.

`cash.forecast` is pure arithmetic over the items it is given (from invoicing.receivables
and payables.due, or any other source), so it never reads another capability's tables.
Weeks start on Monday. Anything due before this week counts in this week; anything past
the horizon is totalled as `later`. Items in another currency are listed, not converted.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from biz_kernel.db import utcnow
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy.orm import Session

CAP = "cash"
CENT = Decimal("0.01")
BELOW_FLOOR = "cash_below_floor"
_NUM: dict[str, Any] = {"type": ["number", "string"]}
_ITEMS: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["amount", "currency"],
        "properties": {"amount": _NUM, "currency": {"type": "string"}, "due": {}},
    },
}


def _dec(value: Any, what: str, *, signed: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{what} {value!r} is not a number")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{what} {value!r} is not a number") from None
    if not out.is_finite() or (out < 0 and not signed):
        raise ValueError(
            f"{what} {value!r} must be finite" + ("" if signed else " and not negative")
        )
    return out.quantize(CENT, rounding=ROUND_HALF_UP)


def _day(value: Any) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).date()


def forecast(
    opening_balance: Any,
    currency: str,
    receivables: list[dict[str, Any]],
    payables: list[dict[str, Any]],
    weeks: int = 13,
    floor: Any = None,
    today: str | None = None,
) -> dict[str, Any]:
    start_day = _day(today) or utcnow().date()
    start = start_day - timedelta(days=start_day.weekday())
    inflow = [Decimal(0)] * weeks
    outflow = [Decimal(0)] * weeks
    later = {"in": Decimal(0), "out": Decimal(0)}
    other: list[dict[str, Any]] = []
    for items, bucket, key in ((receivables, inflow, "in"), (payables, outflow, "out")):
        for item in items:
            if item["currency"] != currency:
                other.append({**item, "direction": key})
                continue
            amount = _dec(item["amount"], "amount")
            due = _day(item.get("due")) or start  # no due date: assume now
            week = max((due - start).days // 7, 0)
            if week >= weeks:
                later[key] += amount
            else:
                bucket[week] += amount
    balance = _dec(opening_balance, "opening_balance", signed=True)
    rows = []
    for n in range(weeks):
        balance += inflow[n] - outflow[n]
        rows.append(
            {
                "week_start": (start + timedelta(weeks=n)).isoformat(),
                "inflow": str(inflow[n]),
                "outflow": str(outflow[n]),
                "closing": str(balance),
            }
        )
    low = min(rows, key=lambda r: Decimal(r["closing"]))
    signals = []
    if floor is not None and Decimal(low["closing"]) < _dec(floor, "floor", signed=True):
        signals.append(BELOW_FLOOR)
    return {
        "currency": currency,
        "weeks": rows,
        "lowest": {"week_start": low["week_start"], "closing": low["closing"]},
        "later": {k: str(v) for k, v in later.items()},
        "other_currency": other,
        "signals": signals,
    }


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    accounting = ports["accounting"]

    def forecast_tool(session: Session, **args: Any) -> dict[str, Any]:
        return forecast(**args)

    def bank_balance(session: Session) -> dict[str, Any]:
        """The accounting system's bank balance: the forecast's opening balance."""
        amount, currency = asyncio.run(accounting.bank_balance())
        return {"balance": str(_dec(amount, "balance", signed=True)), "currency": currency}

    registry.register(
        Tool(
            "cash.bank_balance",
            CAP,
            SideEffect.READ,
            {"type": "object", "additionalProperties": False, "properties": {}},
            bank_balance,
            "Bank balance from the accounting system, to open the forecast with",
        )
    )

    registry.register(
        Tool(
            "cash.forecast",
            CAP,
            SideEffect.READ,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["opening_balance", "currency", "receivables", "payables"],
                "properties": {
                    "opening_balance": _NUM,
                    "currency": {"type": "string"},
                    "receivables": _ITEMS,
                    "payables": _ITEMS,
                    "weeks": {"type": "integer", "minimum": 1, "maximum": 52},
                    "floor": _NUM,
                    "today": {"type": "string"},
                },
            },
            forecast_tool,
            "Week-by-week cash forecast from receivables and payables; flags a floor breach",
        )
    )
