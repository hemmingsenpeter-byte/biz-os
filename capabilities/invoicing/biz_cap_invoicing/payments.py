"""Payment reconciliation: pull payments from accounting and publish payment.received once.

Every poll looks back a fixed window (`invoicing.payment_lookback_days`, default 35) rather
than from a cursor, because a payment can be entered with an earlier date than the last
one seen. The external payment id dedupes. A payment against one of our posted invoices
adds to its paid total; at or past the total the invoice becomes `paid`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from biz_kernel import db
from biz_kernel.audit.log import record
from sqlalchemy import select

from biz_cap_invoicing.models import Invoice, ReceivedPayment
from biz_cap_invoicing.tools import _minor

log = logging.getLogger(__name__)
DEFAULT_LOOKBACK_DAYS = 35


def _lookback(rt: Any) -> timedelta:
    cfg = (rt.manifest.policy or {}).get("invoicing") or {}
    return timedelta(days=float(cfg.get("payment_lookback_days", DEFAULT_LOOKBACK_DAYS)))


def poll_payments(rt: Any) -> int:
    accounting = rt.ports["accounting"]
    payments = asyncio.run(accounting.list_payments(db.utcnow() - _lookback(rt)))
    new = 0
    with db.session_scope() as s:
        for p in payments:
            try:
                with s.begin_nested():
                    seen = s.scalar(
                        select(ReceivedPayment).where(
                            ReceivedPayment.system == p.ref.system,
                            ReceivedPayment.external_id == p.ref.id,
                        )
                    )
                    if seen is not None:
                        continue
                    inv = None
                    if p.invoice_ref is not None:
                        inv = s.scalar(
                            select(Invoice)
                            .where(
                                Invoice.system == p.invoice_ref.system,
                                Invoice.external_id == p.invoice_ref.id,
                            )
                            .with_for_update()
                        )
                    row = ReceivedPayment(
                        system=p.ref.system,
                        external_id=p.ref.id,
                        invoice_id=inv.id if inv is not None else None,
                        amount=Decimal(p.amount),
                        currency=p.currency,
                        paid_at=p.paid_at,
                    )
                    s.add(row)
                    s.flush()
                    payload: dict[str, Any] = {
                        "payment_id": str(row.id),
                        "amount_minor": _minor(Decimal(p.amount)),
                        "currency": p.currency,
                    }
                    if inv is not None:
                        inv.paid = Decimal(inv.paid) + Decimal(p.amount)
                        if inv.paid >= inv.total:
                            inv.status = "paid"
                        payload["invoice_id"] = str(inv.id)
                    rt.bus.publish(
                        s,
                        type="payment.received",
                        from_part="invoicing",
                        summary=f"Payment {p.amount} {p.currency}"
                        + (
                            f" for {inv.reference}" if inv is not None else " (no matching invoice)"
                        ),
                        payload=payload,
                        job_id=inv.job_id if inv is not None else None,
                        idempotency_key=f"payment.received:{p.ref.system}:{p.ref.id}"[:200],
                    )
                    record(s, "invoicing", "invoicing.payment", str(row.id), external_id=p.ref.id)
                    new += 1
            except Exception:
                log.exception("payment %r skipped", p.ref.id[:80])
    return new
