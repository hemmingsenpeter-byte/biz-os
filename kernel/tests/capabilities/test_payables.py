"""Payables: bills match POs, post once at the stated amount, and bank details change only
by a verified call-back that the owner approves."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from biz_cap_payables import config
from biz_cap_payables.models import BankAccount
from biz_cap_payables.tools import register
from biz_kernel.approvals import autonomy, staged
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.records.models import Party, Person
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeAccounting
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stack() -> Any:
    config.bind(None)
    reg = ToolRegistry()
    acct = FakeAccounting()
    register(reg, {"accounting": acct})
    return reg, acct


def _call(reg: ToolRegistry, session: Session, tool: str, **args: Any) -> Any:
    return reg.get(tool).fn(session=session, **args)


def _supplier(session: Session, phone: str | None = "+64 21 555 0100") -> str:
    party = Party(kind="organisation", name="Supplier Ltd")
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name="Sam", phone=phone))
    session.flush()
    return str(party.id)


def _po(
    reg: ToolRegistry, session: Session, supplier: str, number: str = "PO-1", total: Any = 1200
) -> str:
    po_id = str(uuid.uuid4())
    _call(
        reg,
        session,
        "payables.po_record",
        po_id=po_id,
        supplier_party_id=supplier,
        number=number,
        total=total,
        currency="NZD",
    )
    return po_id


def _bill(
    reg: ToolRegistry,
    session: Session,
    supplier: str,
    total: Any,
    number: str = "B-1",
    po: str | None = "PO-1",
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "supplier_party_id": supplier,
        "number": number,
        "total": total,
        "currency": "NZD",
    }
    if po:
        args["po_number"] = po
    return _call(reg, session, "payables.bill_record", **args)  # type: ignore[no-any-return]


def test_bill_matches_its_po_and_posts_once(session: Session, stack: Any) -> None:
    reg, acct = stack
    sup = _supplier(session)
    po = _po(reg, session, sup)
    bill = _bill(reg, session, sup, "1200.00")
    out = _call(reg, session, "payables.bill_match", bill_id=bill["bill_id"])
    assert out["status"] == "matched" and out["po_id"] == po and out["signals"] == []
    with pytest.raises(ValueError, match="does not match"):
        _call(
            reg,
            session,
            "payables.bill_post",
            bill_id=bill["bill_id"],
            amount="1199",
            idempotency_key="k",
        )
    posted = _call(
        reg,
        session,
        "payables.bill_post",
        bill_id=bill["bill_id"],
        amount="1200",
        idempotency_key="k",
    )
    again = _call(
        reg,
        session,
        "payables.bill_post",
        bill_id=bill["bill_id"],
        amount="1200",
        idempotency_key="k",
    )
    assert posted["status"] == "posted" and again["replayed"] and len(acct.bills) == 1


def test_bills_past_the_po_total_are_a_mismatch(session: Session, stack: Any) -> None:
    reg, _ = stack
    sup = _supplier(session)
    _po(reg, session, sup)
    first = _bill(reg, session, sup, 1000, "B-1")
    assert (
        _call(reg, session, "payables.bill_match", bill_id=first["bill_id"])["status"] == "matched"
    )
    second = _bill(reg, session, sup, 300, "B-2")
    out = _call(reg, session, "payables.bill_match", bill_id=second["bill_id"])
    assert out["status"] == "mismatch" and out["signals"] == ["bill_mismatch"]
    assert "over the PO total" in out["problems"][0]
    with pytest.raises(ValueError, match="not matched"):
        _call(
            reg,
            session,
            "payables.bill_post",
            bill_id=second["bill_id"],
            amount=300,
            idempotency_key="k2",
        )


def test_unknown_po_and_ambiguous_total_are_mismatches(session: Session, stack: Any) -> None:
    reg, _ = stack
    sup = _supplier(session)
    _po(reg, session, sup, "PO-1", 500)
    _po(reg, session, sup, "PO-2", 500)
    unknown = _bill(reg, session, sup, 500, "B-1", po="PO-9")
    assert (
        _call(reg, session, "payables.bill_match", bill_id=unknown["bill_id"])["status"]
        == "mismatch"
    )
    ambiguous = _bill(reg, session, sup, 500, "B-2", po=None)
    assert (
        _call(reg, session, "payables.bill_match", bill_id=ambiguous["bill_id"])["status"]
        == "mismatch"
    )


def test_bank_change_needs_the_number_on_record(session: Session, stack: Any) -> None:
    reg, _ = stack
    sup = _supplier(session)
    req = _call(
        reg,
        session,
        "payables.bank_change_request",
        supplier_party_id=sup,
        account="12-3456-7890123-00",
        account_name="Supplier Ltd",
        source="email from accounts@supplier.example (phone in email: +64 9 999 9999)",
    )
    assert req["callback_phone"] == "+64 21 555 0100" and req["signals"] == [
        "bank_change_requested"
    ]
    assert req["account"].endswith("2300") and "1234567890" not in req["account"]
    with pytest.raises(ValueError, match="not the supplier's number"):
        _call(
            reg,
            session,
            "payables.bank_change_confirm",
            change_id=req["change_id"],
            called_number="+64 9 999 9999",
            verified_by="owner",
            idempotency_key="k",
        )
    assert session.get(BankAccount, uuid.UUID(sup)) is None
    out = _call(
        reg,
        session,
        "payables.bank_change_confirm",
        change_id=req["change_id"],
        called_number="+64215550100",
        verified_by="owner",
        idempotency_key="k",
    )
    assert out["status"] == "applied"
    acct = session.get(BankAccount, uuid.UUID(sup))
    assert acct is not None and acct.account == "123456789012300"


def test_no_phone_on_record_cannot_be_confirmed(session: Session, stack: Any) -> None:
    reg, _ = stack
    sup = _supplier(session, phone=None)
    req = _call(
        reg,
        session,
        "payables.bank_change_request",
        supplier_party_id=sup,
        account="123456789",
        account_name="S",
        source="email",
    )
    assert req["problems"]
    with pytest.raises(ValueError, match="no phone number"):
        _call(
            reg,
            session,
            "payables.bank_change_confirm",
            change_id=req["change_id"],
            called_number="",
            verified_by="owner",
            idempotency_key="k",
        )


def test_bank_change_confirm_needs_approval_even_at_level_4(session: Session, stack: Any) -> None:
    reg, _ = stack
    pol = PolicyEngine(
        {"autonomy": {"default": 4}, "limits": {"max_amount": {"default": 1000000}}}, reg
    )
    autonomy.set_level(session, "bookkeeper", "payables.bank_change_confirm", 4, "owner")
    sup = _supplier(session)
    req = _call(
        reg,
        session,
        "payables.bank_change_request",
        supplier_party_id=sup,
        account="123456789",
        account_name="S",
        source="email",
    )
    args = {
        "change_id": req["change_id"],
        "called_number": "+64215550100",
        "verified_by": "owner",
        "idempotency_key": "k",
    }
    op = staged.run_or_stage(session, reg, pol, "bookkeeper", "payables.bank_change_confirm", args)
    assert isinstance(op, staged.StagedOperation)
    assert session.get(BankAccount, uuid.UUID(sup)) is None


def test_no_role_pack_may_confirm_bank_changes() -> None:
    for role in (ROOT / "roles").glob("*/role.yaml"):
        tools = {t for topic in yaml.safe_load(role.read_text())["topics"] for t in topic["tools"]}
        assert "payables.bank_change_confirm" not in tools, role


def test_payment_run_only_includes_verified_suppliers(session: Session, stack: Any) -> None:
    reg, _ = stack
    verified, unverified = _supplier(session), _supplier(session)
    for sup in (verified, unverified):
        _po(reg, session, sup)
        bill = _bill(reg, session, sup, 1200)
        _call(reg, session, "payables.bill_match", bill_id=bill["bill_id"])
        _call(
            reg,
            session,
            "payables.bill_post",
            bill_id=bill["bill_id"],
            amount=1200,
            idempotency_key=f"k-{sup}",
        )
    req = _call(
        reg,
        session,
        "payables.bank_change_request",
        supplier_party_id=verified,
        account="123456789",
        account_name="S",
        source="onboarding form",
    )
    _call(
        reg,
        session,
        "payables.bank_change_confirm",
        change_id=req["change_id"],
        called_number="+64 21 555 0100",
        verified_by="owner",
        idempotency_key="kc",
    )
    run = _call(reg, session, "payables.prepare_run", currency="NZD")
    assert len(run["bill_ids"]) == 1 and run["total_minor"] == 120000
    assert len(run["skipped"]) == 1 and "not verified" in run["skipped"][0]["why"]
    assert _call(reg, session, "payables.prepare_run", currency="NZD")["bill_ids"] == []  # once
