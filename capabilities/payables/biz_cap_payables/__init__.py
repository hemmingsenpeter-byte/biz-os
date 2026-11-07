"""payables: bill matching against POs, draft bills, payment runs, verified bank details."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_payables import config
from biz_cap_payables.models import BankAccount, BankChange, Bill, PurchaseOrder
from biz_cap_payables.tools import register

__all__ = [
    "BankAccount",
    "BankChange",
    "Bill",
    "PurchaseOrder",
    "owner_routes",
    "pollers",
    "register",
]


def owner_routes(rt: Any) -> Any:
    """Hook: owner-only bank change routes, mounted under /v1 (see biz_cap_payables.web)."""
    from biz_cap_payables.web import build_owner_router

    return build_owner_router(rt)


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds composition config (match tolerance); nothing to poll."""
    config.bind(rt.manifest)
    return []
