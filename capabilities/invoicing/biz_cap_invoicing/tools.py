"""Invoicing tools: stages and milestone invoices are drafts; posting is external (gated).

Posting creates a draft invoice in the accounting system, once per idempotency key. The
caller must state the amount, and it must match the stored total, so the approver and
the policy gate see the exact figure that is posted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.contracts.ports import AccountingPort, InvoiceDraft, LineItem
from biz_kernel.contracts.ports import Party as PortParty
from biz_kernel.db import utcnow
from biz_kernel.policy.types import SideEffect
from biz_kernel.records.models import Job, Party, Person
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from biz_cap_invoicing import config
from biz_cap_invoicing.models import Invoice, Stage

CAP = "invoicing"
CENT = Decimal("0.01")
HUNDRED = Decimal(100)
NOT_READY = "invoice_not_ready"
NO_CONTACT = "no_contact_email"
_STR: dict[str, Any] = {"type": "string"}
_UUID: dict[str, Any] = {"type": "string", "format": "uuid"}
_NUM: dict[str, Any] = {"type": ["number", "string"]}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": props,
    }


def _dec(value: Any, what: str) -> Decimal:
    """A finite, non-negative Decimal; anything else (bool, NaN, text) is refused."""
    if isinstance(value, bool):
        raise ValueError(f"{what} {value!r} is not a number")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{what} {value!r} is not a number") from None
    if not out.is_finite() or out < 0:
        raise ValueError(f"{what} {value!r} must be finite and not negative")
    return out


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _minor(value: Decimal) -> int:
    return int(_money(value) * 100)


def _view(inv: Invoice, **extra: Any) -> dict[str, Any]:
    return {
        "invoice_id": str(inv.id),
        "job_id": str(inv.job_id),
        "party_id": str(inv.party_id),
        "stage": inv.stage,
        "reference": inv.reference,
        "currency": inv.currency,
        "amount": str(_money(inv.total)),
        "total_minor": _minor(inv.total),
        "status": inv.status,
        "paid": str(_money(inv.paid)),
        "external_id": inv.external_id,
        "due": inv.due.isoformat() if inv.due else None,
        "signals": [],
        **extra,
    }


def _stage_amount(stage: Stage, job: Job) -> Decimal | None:
    if stage.amount is not None:
        return _money(stage.amount)
    if stage.pct is None or job.value_amount is None:
        return None
    return _money(Decimal(job.value_amount) * stage.pct / HUNDRED)


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    accounting: AccountingPort = ports["accounting"]

    def stages_set(session: Session, job_id: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
        jid = uuid.UUID(job_id)
        if session.get(Job, jid) is None:
            raise LookupError(f"job {job_id} not found")
        if session.scalar(select(func.count()).where(Invoice.job_id == jid)):
            raise ValueError("this job already has invoices; its stages can't change")
        rows, names, total_pct = [], set(), Decimal(0)
        for n, s in enumerate(stages):
            name = s["name"].strip()
            if not name or name in names:
                raise ValueError(f"stage name {name!r} is empty or repeated")
            names.add(name)
            if ("pct" in s) == ("amount" in s):
                raise ValueError(f"stage {name!r} needs exactly one of pct or amount")
            pct = _dec(s["pct"], "pct") if "pct" in s else None
            amount = _dec(s["amount"], "amount") if "amount" in s else None
            if pct is not None:
                total_pct += pct
            rows.append(Stage(job_id=jid, name=name, position=n, pct=pct, amount=amount))
        if total_pct > HUNDRED:
            raise ValueError(f"stage shares add up to {total_pct}%, over 100%")
        session.execute(delete(Stage).where(Stage.job_id == jid))
        session.add_all(rows)
        session.flush()
        record(session, CAP, "invoicing.stages_set", job_id, stages=len(rows))
        return {"job_id": job_id, "stages": [r.name for r in rows]}

    def milestone_invoice(
        session: Session, job_id: str, milestone_id: str, name: str
    ) -> dict[str, Any]:
        mid = uuid.UUID(milestone_id)
        existing = session.scalar(select(Invoice).where(Invoice.milestone_id == mid))
        if existing is not None:
            return _view(existing, replayed=True)
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise LookupError(f"job {job_id} not found")
        problems = []
        stage = session.scalar(select(Stage).where(Stage.job_id == job.id, Stage.name == name))
        if stage is None:
            problems.append(f"no payment stage named {name!r} on this job")
        if job.client_party_id is None:
            problems.append("the job has no client")
        if not job.currency:
            problems.append("the job has no currency")
        amount = _stage_amount(stage, job) if stage is not None else None
        if stage is not None and amount is None:
            problems.append(f"stage {name!r} is a share of the job value, which is not set")
        if amount is not None and job.value_amount is not None:
            billed = session.scalar(
                select(func.coalesce(func.sum(Invoice.total), 0)).where(Invoice.job_id == job.id)
            )
            if Decimal(billed) + amount > Decimal(job.value_amount):
                problems.append(
                    f"invoicing {amount} would take the job past its value {job.value_amount}"
                )
        if problems:
            # Nothing is drafted; the role escalates on the signal.
            return {"invoice_id": None, "signals": [NOT_READY], "problems": problems}
        assert stage is not None and amount is not None and job.client_party_id is not None
        inv = Invoice(
            job_id=job.id,
            party_id=job.client_party_id,
            milestone_id=mid,
            stage=stage.name,
            reference="",
            currency=str(job.currency),
            lines=[{"description": stage.name, "quantity": "1", "unit_amount": str(amount)}],
            total=amount,
            due=utcnow() + timedelta(days=int(config.setting("payment_terms_days", 20))),
        )
        session.add(inv)
        session.flush()
        inv.reference = f"INV-{inv.id.hex[-8:].upper()}"
        session.flush()
        record(session, CAP, "invoicing.drafted", str(inv.id), job_id=job.id, total=str(amount))
        return _view(inv)

    def invoice_post(
        session: Session, invoice_id: str, amount: Any, idempotency_key: str
    ) -> dict[str, Any]:
        inv = session.get(Invoice, uuid.UUID(invoice_id), with_for_update=True)
        if inv is None:
            raise LookupError(f"invoice {invoice_id} not found")
        if inv.status != "draft":  # posted or paid
            if inv.idempotency_key == idempotency_key:
                return _view(inv, replayed=True)
            raise ValueError(f"invoice {inv.reference} is already posted")
        if _money(_dec(amount, "amount")) != _money(inv.total):
            raise ValueError(f"amount {amount} does not match the invoice total {inv.total}")
        party = session.get(Party, inv.party_id)
        if party is None:
            raise LookupError(f"party {inv.party_id} not found")
        email = session.scalar(
            select(Person.email)
            .where(Person.party_id == party.id, Person.email.is_not(None))
            .order_by(Person.id)
            .limit(1)
        )
        who = PortParty(id=party.id, name=party.name, email=email)
        draft = InvoiceDraft(
            party=who,
            reference=inv.reference,
            currency=inv.currency,
            lines=[
                LineItem(
                    description=line["description"],
                    quantity=Decimal(line["quantity"]),
                    unit_amount=Decimal(line["unit_amount"]),
                )
                for line in inv.lines
            ],
            due=inv.due,
            idempotency_key=idempotency_key,  # the port posts once per key
        )

        async def post() -> Any:
            await accounting.upsert_contact(who)
            return await accounting.create_invoice(draft)

        ref = asyncio.run(post())
        inv.status, inv.posted_at = "posted", utcnow()
        inv.idempotency_key, inv.system, inv.external_id = idempotency_key, ref.system, ref.id
        session.flush()
        record(session, CAP, "invoicing.posted", str(inv.id), external_id=ref.id)
        return _view(inv, url=ref.url)

    def receivables(session: Session) -> dict[str, Any]:
        rows = session.scalars(select(Invoice).where(Invoice.status == "posted"))
        return {
            "items": [
                {
                    "invoice_id": str(i.id),
                    "due": i.due.isoformat() if i.due else None,
                    "amount": str(_money(i.total - i.paid)),
                    "currency": i.currency,
                }
                for i in rows
            ]
        }

    def reminder_draft(session: Session, invoice_id: str, reminder: int) -> dict[str, Any]:
        """A plain, deterministic payment reminder for the invoice's client contact."""
        inv = session.get(Invoice, uuid.UUID(invoice_id))
        if inv is None:
            raise LookupError(f"invoice {invoice_id} not found")
        email = session.scalar(
            select(Person.email)
            .where(Person.party_id == inv.party_id, Person.email.is_not(None))
            .order_by(Person.id)
            .limit(1)
        )
        if not email:
            return {"invoice_id": invoice_id, "signals": [NO_CONTACT], "problems": ["no email"]}
        owed = _money(inv.total - inv.paid)
        due = inv.due.date().isoformat() if inv.due else "its due date"
        lead = "A reminder" if reminder <= 1 else f"Reminder {reminder}"
        return {
            "invoice_id": invoice_id,
            "party_id": str(inv.party_id),
            "to": email,
            "subject": f"{lead}: invoice {inv.reference} is overdue",
            "body": (
                f"Hello, invoice {inv.reference} for {owed} {inv.currency} was due on {due} "
                "and is still unpaid. If you have already paid, thank you, and please ignore "
                "this note. Otherwise, could you let us know when to expect payment?"
            ),
            "signals": [],
        }

    def invoice_read(session: Session, invoice_id: str) -> dict[str, Any]:
        inv = session.get(Invoice, uuid.UUID(invoice_id))
        if inv is None:
            raise LookupError(f"invoice {invoice_id} not found")
        return _view(inv, lines=inv.lines)

    stage_schema = _obj({"name": _STR, "pct": _NUM, "amount": _NUM}, ["name"])
    for tool in (
        Tool(
            "invoicing.stages_set",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _UUID,
                    "stages": {"type": "array", "minItems": 1, "items": stage_schema},
                },
                ["job_id", "stages"],
            ),
            stages_set,
            "Set a job's payment stages (share of job value or fixed amount)",
        ),
        Tool(
            "invoicing.milestone_invoice",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"job_id": _UUID, "milestone_id": _UUID, "name": _STR},
                ["job_id", "milestone_id", "name"],
            ),
            milestone_invoice,
            "Draft the invoice for a reached milestone from its payment stage, once per milestone",
        ),
        Tool(
            "invoicing.invoice_post",
            CAP,
            SideEffect.EXTERNAL,
            _obj(
                {"invoice_id": _UUID, "amount": _NUM, "idempotency_key": _STR},
                ["invoice_id", "amount", "idempotency_key"],
            ),
            invoice_post,
            "Post a drafted invoice to the accounting system as a draft, once per key",
        ),
        Tool(
            "invoicing.receivables",
            CAP,
            SideEffect.READ,
            _obj({}),
            receivables,
            "Posted invoices still owed to us, with due dates and outstanding amounts",
        ),
        Tool(
            "invoicing.reminder_draft",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"invoice_id": _UUID, "reminder": {"type": "integer", "minimum": 1}},
                ["invoice_id", "reminder"],
            ),
            reminder_draft,
            "Draft a payment reminder for an overdue invoice to the client's contact",
        ),
        Tool(
            "invoicing.invoice_read",
            CAP,
            SideEffect.READ,
            _obj({"invoice_id": _UUID}, ["invoice_id"]),
            invoice_read,
            "Read an invoice and its lines",
        ),
    ):
        registry.register(tool)
