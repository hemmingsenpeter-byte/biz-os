"""invoicing: payment stages, milestone invoices posted to accounting as drafts, payments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_invoicing.models import Invoice, ReceivedPayment, Stage
from biz_cap_invoicing.tools import register

__all__ = ["Invoice", "ReceivedPayment", "Stage", "pollers", "register"]


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: pull payments from the accounting port and publish payment.received."""
    from biz_cap_invoicing.payments import poll_payments

    return [lambda: poll_payments(rt)]
