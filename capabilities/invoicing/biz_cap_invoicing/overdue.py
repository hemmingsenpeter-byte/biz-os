"""Debtor sequence: publish invoice.overdue once per reminder step for an unpaid invoice.

Steps are days past due (`invoicing.reminder_days`, default [7, 14, 30]). Each poll
publishes at most one reminder per invoice: the latest step now due. Steps that came due
while the poller wasn't running are skipped, not caught up, so a customer never gets
several reminders at once. Nothing is published while the kill switch is on; the dates
are re-read after the resume. Each step is keyed so it is never published twice. What the
reminder says and whether it is sent is the role's SOP, through comms and its gate.
"""

from __future__ import annotations

import logging
from typing import Any

from biz_kernel import control, db
from sqlalchemy import select

from biz_cap_invoicing import config
from biz_cap_invoicing.models import Invoice
from biz_cap_invoicing.tools import _minor

log = logging.getLogger(__name__)
DEFAULT_STEPS = [7, 14, 30]


def poll_overdue(rt: Any) -> int:
    steps = sorted(int(d) for d in config.setting("reminder_days", DEFAULT_STEPS))
    now = db.utcnow()
    new = 0
    with db.session_scope() as s:
        if control.is_paused(s):
            return 0
        rows = s.scalars(
            select(Invoice)
            .where(Invoice.status == "posted", Invoice.due.is_not(None), Invoice.due < now)
            .with_for_update(skip_locked=True)
        )
        for inv in rows:
            assert inv.due is not None
            days = (now - inv.due).days
            step = sum(1 for d in steps if days >= d)  # the latest step now due
            if step <= inv.reminders_sent:
                continue
            rt.bus.publish(
                s,
                type="invoice.overdue",
                from_part="invoicing",
                summary=f"Invoice {inv.reference} is {days} days overdue (reminder {step})",
                payload={
                    "invoice_id": str(inv.id),
                    "party_id": str(inv.party_id),
                    "days_overdue": days,
                    "reminder": step,
                    "outstanding_minor": _minor(inv.total - inv.paid),
                    "currency": inv.currency,
                },
                job_id=inv.job_id,
                idempotency_key=f"invoice.overdue:{inv.id}:{step}",
            )
            inv.reminders_sent = step
            new += 1
    return new
