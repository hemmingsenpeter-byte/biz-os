"""Cash forecast: weekly buckets, overdue counts now, horizon, currency, floor signal."""

from __future__ import annotations

from decimal import Decimal

import pytest
from biz_cap_cash import forecast

TODAY = "2026-11-11"  # a Wednesday; week 0 starts Monday 2026-11-09


def test_weekly_buckets_and_running_balance() -> None:
    out = forecast(
        opening_balance="1000",
        currency="NZD",
        receivables=[
            {"amount": "500", "currency": "NZD", "due": "2026-11-20"},  # week 1
            {"amount": "200", "currency": "NZD", "due": "2026-10-01"},  # overdue: week 0
        ],
        payables=[{"amount": "1500", "currency": "NZD", "due": "2026-11-10"}],  # week 0
        today=TODAY,
    )
    weeks = out["weeks"]
    assert len(weeks) == 13 and weeks[0]["week_start"] == "2026-11-09"
    assert weeks[0]["inflow"] == "200.00" and weeks[0]["outflow"] == "1500.00"
    assert weeks[0]["closing"] == "-300.00" and weeks[1]["closing"] == "200.00"
    assert out["lowest"] == {"week_start": "2026-11-09", "closing": "-300.00"}
    assert out["signals"] == []


def test_horizon_currency_and_floor() -> None:
    out = forecast(
        opening_balance=100,
        currency="NZD",
        receivables=[
            {"amount": 50, "currency": "NZD", "due": "2027-06-01"},
            {"amount": 999, "currency": "AUD", "due": "2026-11-12"},
        ],
        payables=[{"amount": 80, "currency": "NZD"}],  # no due date: now
        floor=50,
        today=TODAY,
    )
    assert out["later"] == {"in": "50.00", "out": "0"}
    assert len(out["other_currency"]) == 1 and Decimal(out["weeks"][-1]["closing"]) == 20
    assert out["signals"] == ["cash_below_floor"]


@pytest.mark.parametrize("bad", [-1, "NaN", True, "x"])
def test_bad_amounts_are_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        forecast(1, "NZD", [{"amount": bad, "currency": "NZD"}], [], today=TODAY)
