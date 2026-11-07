"""payables: bill matching against POs, draft bills, payment runs, verified bank details."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_payables import config
from biz_cap_payables.models import BankAccount, BankChange, Bill, PurchaseOrder
from biz_cap_payables.tools import register

__all__ = ["BankAccount", "BankChange", "Bill", "PurchaseOrder", "pollers", "register"]


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds composition config (match tolerance); nothing to poll."""
    config.bind(rt.manifest)
    return []
