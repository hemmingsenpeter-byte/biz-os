"""Payables tools: record and match bills against POs, post them as drafts, payment runs,
and supplier bank details that change only by a verified call-back.

A bank change is two steps. `bank_change_request` records it as pending and returns the
supplier's phone number from our own records (never from the request). Only
`bank_change_confirm` writes the account, and only when the number called matches that
record. It is a money action with no amount, so policy sends it to the owner at every
autonomy level, and no role pack may list it.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.contracts.ports import AccountingPort, BillDraft, LineItem
from biz_kernel.contracts.ports import Party as PortParty
from biz_kernel.db import utcnow
from biz_kernel.policy.types import SideEffect
from biz_kernel.records.models import Party, Person
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biz_cap_payables import config
from biz_cap_payables.models import BankAccount, BankChange, Bill, PurchaseOrder

CAP = "payables"
CENT = Decimal("0.01")
MISMATCH = "bill_mismatch"
BANK_CHANGE = "bank_change_requested"
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
    if isinstance(value, bool):
        raise ValueError(f"{what} {value!r} is not a number")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{what} {value!r} is not a number") from None
    if not out.is_finite() or out < 0:
        raise ValueError(f"{what} {value!r} must be finite and not negative")
    return out.quantize(CENT, rounding=ROUND_HALF_UP)


def _minor(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def _mask(account: str) -> str:
    return "*" * max(len(account) - 4, 0) + account[-4:]


def _bill_view(b: Bill, **extra: Any) -> dict[str, Any]:
    return {
        "bill_id": str(b.id),
        "supplier_party_id": str(b.supplier_party_id),
        "number": b.number,
        "po_id": str(b.po_id) if b.po_id else None,
        "amount": str(b.total),
        "total_minor": _minor(b.total),
        "currency": b.currency,
        "status": b.status,
        "problems": list(b.problems or []),
        "signals": [MISMATCH] if b.status == "mismatch" else [],
        **extra,
    }


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    accounting: AccountingPort = ports["accounting"]

    def po_record(
        session: Session,
        po_id: str,
        supplier_party_id: str,
        number: str,
        currency: str,
        total: Any = None,
        total_minor: int | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """`total` in major units, or `total_minor` as po.issued carries it; exactly one."""
        if (total is None) == (total_minor is None):
            raise ValueError("give exactly one of total and total_minor")
        if total is None:
            total = Decimal(int(total_minor or 0)) / 100
        po = session.get(PurchaseOrder, uuid.UUID(po_id))
        if po is None:
            po = PurchaseOrder(
                id=uuid.UUID(po_id),
                supplier_party_id=uuid.UUID(supplier_party_id),
                number=number.strip(),
                job_id=uuid.UUID(job_id) if job_id else None,
                total=_dec(total, "total"),
                currency=currency,
            )
            session.add(po)
            session.flush()
        return {"po_id": str(po.id), "number": po.number, "total": str(po.total)}

    def bill_record(
        session: Session,
        supplier_party_id: str,
        number: str,
        total: Any,
        currency: str,
        po_number: str | None = None,
        lines: list[dict[str, Any]] | None = None,
        due: str | None = None,
    ) -> dict[str, Any]:
        supplier = uuid.UUID(supplier_party_id)
        existing = session.scalar(
            select(Bill).where(Bill.supplier_party_id == supplier, Bill.number == number.strip())
        )
        if existing is not None:
            return _bill_view(existing, replayed=True)
        amount = _dec(total, "total")
        clean = [
            {
                "description": str(line["description"]),
                "quantity": str(_dec(line.get("quantity", 1), "quantity")),
                "unit_amount": str(_dec(line["unit_amount"], "unit_amount")),
            }
            for line in lines or [{"description": f"Bill {number}", "unit_amount": amount}]
        ]
        bill = Bill(
            supplier_party_id=supplier,
            number=number.strip(),
            po_number=po_number.strip() if po_number else None,
            lines=clean,
            total=amount,
            currency=currency,
            due=datetime.fromisoformat(due) if due else None,
        )
        session.add(bill)
        session.flush()
        record(session, CAP, "payables.bill_recorded", str(bill.id), total=str(amount))
        return _bill_view(bill)

    def bill_match(session: Session, bill_id: str) -> dict[str, Any]:
        bill = session.get(Bill, uuid.UUID(bill_id), with_for_update=True)
        if bill is None:
            raise LookupError(f"bill {bill_id} not found")
        if bill.status not in ("received", "mismatch"):
            return _bill_view(bill, replayed=True)
        pos = list(
            session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.supplier_party_id == bill.supplier_party_id
                )
            )
        )
        if bill.po_number:
            candidates = [p for p in pos if p.number == bill.po_number]
        else:
            candidates = [p for p in pos if p.total == bill.total]
        problems: list[str] = []
        po = candidates[0] if len(candidates) == 1 else None
        if po is None:
            problems.append(
                f"no single purchase order matches (found {len(candidates)})"
                + (f" for PO {bill.po_number}" if bill.po_number else " by supplier and total")
            )
        else:
            tol = Decimal(str(config.setting("match_tolerance_pct", 0))) / 100
            billed = session.scalar(
                select(func.coalesce(func.sum(Bill.total), 0)).where(
                    Bill.po_id == po.id, Bill.id != bill.id
                )
            )
            if po.currency != bill.currency:
                problems.append(f"currency {bill.currency} differs from the PO's {po.currency}")
            if Decimal(billed) + bill.total > po.total * (1 + tol):
                problems.append(
                    f"bills on PO {po.number} would total {Decimal(billed) + bill.total}, "
                    f"over the PO total {po.total}"
                )
        bill.problems = problems
        bill.status = "mismatch" if problems else "matched"
        bill.po_id = po.id if po is not None and not problems else None
        session.flush()
        record(session, CAP, "payables.bill_match", str(bill.id), status=bill.status)
        return _bill_view(bill)

    def bill_post(
        session: Session, bill_id: str, amount: Any, idempotency_key: str
    ) -> dict[str, Any]:
        bill = session.get(Bill, uuid.UUID(bill_id), with_for_update=True)
        if bill is None:
            raise LookupError(f"bill {bill_id} not found")
        if bill.status in ("posted", "in_run"):
            if bill.idempotency_key == idempotency_key:
                return _bill_view(bill, replayed=True)
            raise ValueError(f"bill {bill.number} is already posted")
        if bill.status != "matched":
            raise ValueError(f"bill {bill.number} is {bill.status}, not matched to a PO")
        if _dec(amount, "amount") != bill.total:
            raise ValueError(f"amount {amount} does not match the bill total {bill.total}")
        party = session.get(Party, bill.supplier_party_id)
        if party is None:
            raise LookupError(f"supplier {bill.supplier_party_id} not found")
        who = PortParty(id=party.id, name=party.name)
        draft = BillDraft(
            party=who,
            reference=bill.number,
            currency=bill.currency,
            lines=[
                LineItem(
                    description=line["description"],
                    quantity=Decimal(line["quantity"]),
                    unit_amount=Decimal(line["unit_amount"]),
                )
                for line in bill.lines
            ],
            due=bill.due,
            idempotency_key=idempotency_key,
        )
        ref = asyncio.run(accounting.create_bill(draft))
        bill.status, bill.idempotency_key = "posted", idempotency_key
        bill.system, bill.external_id = ref.system, ref.id
        session.flush()
        record(session, CAP, "payables.bill_posted", str(bill.id), external_id=ref.id)
        return _bill_view(bill)

    def bank_change_request(
        session: Session, supplier_party_id: str, account: str, account_name: str, source: str
    ) -> dict[str, Any]:
        supplier = uuid.UUID(supplier_party_id)
        if session.get(Party, supplier) is None:
            raise LookupError(f"supplier {supplier_party_id} not found")
        clean = re.sub(r"[\s-]", "", account)
        if not re.fullmatch(r"[0-9A-Za-z]{6,34}", clean):
            raise ValueError("account must be 6-34 letters or digits")
        # The number to call comes from our records, never from the request itself.
        phone = session.scalar(
            select(Person.phone)
            .where(Person.party_id == supplier, Person.phone.is_not(None))
            .order_by(Person.id)
            .limit(1)
        )
        change = BankChange(
            supplier_party_id=supplier,
            account=clean,
            account_name=account_name.strip(),
            source=source[:200],
            callback_phone=phone,
        )
        session.add(change)
        session.flush()
        record(session, CAP, "payables.bank_change_requested", str(change.id), source=source[:200])
        problems = [] if phone else ["no phone number on record for this supplier"]
        return {
            "change_id": str(change.id),
            "account": _mask(clean),
            "callback_phone": phone,
            "problems": problems,
            "signals": [BANK_CHANGE],  # always the owner's call-back task
        }

    def bank_change_confirm(
        session: Session, change_id: str, called_number: str, verified_by: str, idempotency_key: str
    ) -> dict[str, Any]:
        change = session.get(BankChange, uuid.UUID(change_id), with_for_update=True)
        if change is None:
            raise LookupError(f"bank change {change_id} not found")
        if change.status == "applied":
            return {"change_id": change_id, "status": "applied", "replayed": True}
        if not change.callback_phone:
            raise ValueError("no phone number on record: add one before confirming")
        if _digits(called_number) != _digits(change.callback_phone):
            raise ValueError("the number called is not the supplier's number on record")
        if not verified_by.strip():
            raise ValueError("say who made the call-back")
        now = utcnow()
        acct = session.get(BankAccount, change.supplier_party_id)
        if acct is None:
            acct = BankAccount(supplier_party_id=change.supplier_party_id)
            session.add(acct)
        acct.account, acct.account_name = change.account, change.account_name
        acct.change_id, acct.verified_at = change.id, now
        change.status, change.verified_by, change.applied_at = "applied", verified_by, now
        session.flush()
        record(
            session,
            CAP,
            "payables.bank_change_applied",
            str(change.id),
            supplier=str(change.supplier_party_id),
            verified_by=verified_by,
        )
        return {"change_id": change_id, "status": "applied", "account": _mask(change.account)}

    def prepare_run(session: Session, currency: str) -> dict[str, Any]:
        bills = list(
            session.scalars(
                select(Bill)
                .where(Bill.status == "posted", Bill.currency == currency)
                .order_by(Bill.due.nulls_last(), Bill.created_at)
                .with_for_update()
            )
        )
        verified = {
            a.supplier_party_id
            for a in session.scalars(
                select(BankAccount).where(
                    BankAccount.supplier_party_id.in_({b.supplier_party_id for b in bills})
                )
            )
        }
        run_id = uuid.uuid4()
        included = [b for b in bills if b.supplier_party_id in verified]
        skipped = [b for b in bills if b.supplier_party_id not in verified]
        for b in included:
            b.status = "in_run"
        session.flush()
        total = sum((b.total for b in included), Decimal(0))
        record(session, CAP, "payables.run_prepared", str(run_id), bills=len(included))
        return {
            "run_id": str(run_id),
            "bill_ids": [str(b.id) for b in included],
            "total_minor": _minor(total),
            "currency": currency,
            "skipped": [
                {"bill_id": str(b.id), "why": "supplier bank details are not verified"}
                for b in skipped
            ],
        }

    def due(session: Session) -> dict[str, Any]:
        rows = session.scalars(select(Bill).where(Bill.status.in_(("posted", "in_run"))))
        return {
            "items": [
                {
                    "bill_id": str(b.id),
                    "due": b.due.isoformat() if b.due else None,
                    "amount": str(b.total),
                    "currency": b.currency,
                }
                for b in rows
            ]
        }

    line = _obj(
        {"description": _STR, "quantity": _NUM, "unit_amount": _NUM}, ["description", "unit_amount"]
    )
    for tool in (
        Tool(
            "payables.po_record",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "po_id": _UUID,
                    "supplier_party_id": _UUID,
                    "number": {"type": "string", "minLength": 1},
                    "total": _NUM,
                    "total_minor": {"type": "integer", "minimum": 0},
                    "currency": _STR,
                    "job_id": {"type": ["string", "null"], "format": "uuid"},
                },
                ["po_id", "supplier_party_id", "number", "currency"],
            ),
            po_record,
            "Keep a copy of an issued purchase order for bill matching",
        ),
        Tool(
            "payables.bill_record",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "supplier_party_id": _UUID,
                    "number": _STR,
                    "total": _NUM,
                    "currency": _STR,
                    "po_number": _STR,
                    "lines": {"type": "array", "items": line},
                    "due": _STR,
                },
                ["supplier_party_id", "number", "total", "currency"],
            ),
            bill_record,
            "Record a supplier bill, once per supplier and bill number",
        ),
        Tool(
            "payables.bill_match",
            CAP,
            SideEffect.DRAFT,
            _obj({"bill_id": _UUID}, ["bill_id"]),
            bill_match,
            "Match a bill to its purchase order; a mismatch raises bill_mismatch",
        ),
        Tool(
            "payables.bill_post",
            CAP,
            SideEffect.EXTERNAL,
            _obj(
                {"bill_id": _UUID, "amount": _NUM, "idempotency_key": _STR},
                ["bill_id", "amount", "idempotency_key"],
            ),
            bill_post,
            "Post a matched bill to accounting as a draft, once per key; the amount must match",
        ),
        Tool(
            "payables.bank_change_request",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"supplier_party_id": _UUID, "account": _STR, "account_name": _STR, "source": _STR},
                ["supplier_party_id", "account", "account_name", "source"],
            ),
            bank_change_request,
            "Record a requested supplier bank change as pending, with the number to call back",
        ),
        Tool(
            "payables.bank_change_confirm",
            CAP,
            SideEffect.MONEY,  # no amount: policy sends it to the owner at every level
            _obj(
                {
                    "change_id": _UUID,
                    "called_number": _STR,
                    "verified_by": _STR,
                    "idempotency_key": _STR,
                },
                ["change_id", "called_number", "verified_by", "idempotency_key"],
            ),
            bank_change_confirm,
            "Apply a bank change after a call-back to the supplier's number on record",
        ),
        Tool(
            "payables.due",
            CAP,
            SideEffect.READ,
            _obj({}),
            due,
            "Posted bills we still owe, with due dates and amounts",
        ),
        Tool(
            "payables.prepare_run",
            CAP,
            SideEffect.DRAFT,
            _obj({"currency": _STR}, ["currency"]),
            prepare_run,
            "Gather posted bills of suppliers with verified bank details into a payment run",
        ),
    ):
        registry.register(tool)
