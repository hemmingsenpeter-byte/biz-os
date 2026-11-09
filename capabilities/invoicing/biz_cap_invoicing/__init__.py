"""invoicing: payment stages, milestone invoices posted to accounting as drafts, payments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_invoicing import config
from biz_cap_invoicing.models import Invoice, ReceivedPayment, Stage
from biz_cap_invoicing.tools import register, reminder_template

__all__ = ["Invoice", "ReceivedPayment", "Stage", "pollers", "register"]


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds config (payment terms, lookback); pulls payments from accounting."""
    from biz_cap_invoicing.overdue import poll_overdue
    from biz_cap_invoicing.payments import poll_payments

    config.bind(rt.manifest)
    reminder_template()  # a bad template fails at start, not on the first reminder
    config.tax_rate()  # so does a bad tax rate
    # Payments first, so an invoice paid since the last poll gets no reminder.
    return [lambda: poll_payments(rt), lambda: poll_overdue(rt)]
