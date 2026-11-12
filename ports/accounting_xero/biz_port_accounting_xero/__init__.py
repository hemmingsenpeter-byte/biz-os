"""Xero adapter for AccountingPort: Xero's REST API over httpx (a custom connection).

Invoices and bills are created as DRAFT only, so nothing is approved or sent to the
customer from here; the owner finishes them in Xero. Each create is guarded twice: a
durable ledger keyed on the caller's idempotency key, and Xero's own Idempotency-Key
header. A create that may have reached Xero ("sending" in the ledger) is reconciled by
looking the reference up in Xero before it is ever retried.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from typing import Any, Protocol

import httpx
from biz_kernel.contracts.ports import (
    BillDraft,
    ExternalRef,
    InvoiceDraft,
    Party,
    Payment,
    PostedDocument,
)
from biz_kernel.db import engine as _db_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .models import XeroPost

__all__ = [
    "MemoryLedger",
    "SqlLedger",
    "XeroAccounting",
    "XeroFailed",
    "XeroUncertain",
    "create",
]

API = "https://api.xero.com/api.xro/2.0"
TOKEN_URL = "https://identity.xero.com/connect/token"
SCOPES = "accounting.transactions accounting.contacts"
RECEIVED, SALES = "ACCRECPAYMENT", "ACCREC"  # Xero's PaymentType and invoice Type for money in
IN_FLIGHT = timedelta(minutes=2)  # a younger "sending" row belongs to a live call


class XeroFailed(Exception):
    """Xero refused the request; nothing was created, so it may be retried."""


class XeroUncertain(Exception):
    """The request may have reached Xero; it is reconciled before any retry."""


class PostLedger(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def claim(self, key: str) -> bool: ...
    def put(self, key: str, provider_id: str) -> None: ...
    def release(self, key: str) -> None: ...


class MemoryLedger:
    """In-memory ledger: lost on restart, so NOT enough for production."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = {"status": "sending", "provider_id": None, "at": datetime.now(UTC)}
            return True

    def put(self, key: str, provider_id: str) -> None:
        with self._lock:
            self._data[key] = {
                "status": "sent",
                "provider_id": provider_id,
                "at": datetime.now(UTC),
            }

    def release(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class SqlLedger:
    """Durable ledger on port_xero_posts; "sending" is recorded before the call."""

    def __init__(self, engine_or_factory: Any) -> None:
        self._factory: Any = (
            sessionmaker(engine_or_factory, expire_on_commit=False)
            if isinstance(engine_or_factory, Engine)
            else engine_or_factory
        )

    def get(self, key: str) -> dict[str, Any] | None:
        with self._factory() as s:
            row = s.get(XeroPost, key)
        if row is None:
            return None
        return {"status": row.status, "provider_id": row.provider_id, "at": row.at}

    def claim(self, key: str) -> bool:
        with self._factory() as s:
            stmt = (
                pg_insert(XeroPost)
                .values(key=key, status="sending", at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(XeroPost.key)
            )
            won = s.execute(stmt).scalar() is not None
            s.commit()
        return won

    def put(self, key: str, provider_id: str) -> None:
        with self._factory() as s:
            row = s.get(XeroPost, key)
            if row is None:
                row = XeroPost(key=key)
                s.add(row)
            row.status, row.provider_id, row.at = "sent", provider_id, datetime.now(UTC)
            s.commit()

    def release(self, key: str) -> None:
        with self._factory() as s:
            row = s.get(XeroPost, key)
            if row is not None and row.status == "sending":
                s.delete(row)
                s.commit()


def _quote(value: str) -> str:
    """A string literal for Xero's `where` filter."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _xero_date(value: Any) -> datetime:
    """Xero JSON dates look like /Date(1700000000000+0000)/."""
    if isinstance(value, str) and value.startswith("/Date("):
        ms = int(value[6:].split(")")[0].split("+")[0].split("-")[0])
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def _posted(row: dict[str, Any]) -> PostedDocument:
    """A Xero invoice row as a PostedDocument, with Xero's own tax and tax-inclusive total."""

    def amount(name: str) -> Decimal | None:
        value = row.get(name)
        return None if value is None else Decimal(str(value))

    tax, total = amount("TotalTax"), amount("Total")
    if tax is None or total is None:  # both or neither
        tax = total = None
    return PostedDocument(system="xero", id=str(row["InvoiceID"]), tax=tax, total=total)


class XeroAccounting:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str | None = None,
        http: httpx.AsyncClient | None = None,
        ledger: PostLedger | None = None,
        sales_account: str = "200",
        purchases_account: str = "300",
    ) -> None:
        self.client_id, self.client_secret, self.tenant_id = client_id, client_secret, tenant_id
        self.sales_account, self.purchases_account = sales_account, purchases_account
        self.ledger: PostLedger = ledger if ledger is not None else MemoryLedger()
        self._injected_http = http
        self._token: tuple[str, float] | None = None

    # -- transport
    async def _send(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, **kw: Any
    ) -> httpx.Response:
        if self._injected_http is not None:
            return await self._injected_http.request(method, url, headers=headers, **kw)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(method, url, headers=headers, **kw)

    async def _access_token(self) -> str:
        if self._token is not None and self._token[1] > time.monotonic() + 60:
            return self._token[0]
        resp = await self._send(
            "POST",
            TOKEN_URL,
            data={"grant_type": "client_credentials", "scope": SCOPES},
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code >= 400:
            raise XeroFailed(f"token {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        self._token = (body["access_token"], time.monotonic() + float(body["expires_in"]))
        return self._token[0]

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {await self._access_token()}",
            "Accept": "application/json",
        }
        if self.tenant_id:
            headers["Xero-tenant-id"] = self.tenant_id
        headers.update(extra or {})
        return await self._send(method, f"{API}{path}", headers=headers, json=json, params=params)

    async def _get(self, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._api("GET", path, **kw)
        if resp.status_code >= 400:
            raise XeroFailed(f"GET {path} {resp.status_code}: {resp.text[:200]}")
        data: dict[str, Any] = resp.json()
        return data

    # -- port
    async def upsert_contact(self, party: Party) -> ExternalRef:
        """Contacts are found by ContactNumber (our party id), so a rename never duplicates."""
        where = f"ContactNumber=={_quote(str(party.id))}"
        found = (await self._get("/Contacts", params={"where": where})).get("Contacts") or []
        if found:
            return ExternalRef(system="xero", id=found[0]["ContactID"])
        body: dict[str, Any] = {"Name": party.name, "ContactNumber": str(party.id)}
        if party.email:
            body["EmailAddress"] = party.email
        resp = await self._api(
            "POST",
            "/Contacts",
            json={"Contacts": [body]},
            extra={"Idempotency-Key": f"contact:{party.id}"},
        )
        if resp.status_code >= 400:
            raise XeroFailed(f"contact {resp.status_code}: {resp.text[:200]}")
        return ExternalRef(system="xero", id=resp.json()["Contacts"][0]["ContactID"])

    async def create_invoice(self, inv: InvoiceDraft) -> PostedDocument:
        return await self._create("ACCREC", inv, self.sales_account)

    async def create_bill(self, bill: BillDraft) -> PostedDocument:
        return await self._create("ACCPAY", bill, self.purchases_account)

    async def list_payments(self, since: datetime) -> list[Payment]:
        headers = {"If-Modified-Since": format_datetime(since.astimezone(UTC), usegmt=True)}
        data = await self._get("/Payments", extra=headers)
        out = []
        for p in data.get("Payments") or []:
            if p.get("Status") != "AUTHORISED":
                continue
            invoice = p.get("Invoice") or {}
            # Only money received on a sales invoice. /Payments also lists what we paid
            # suppliers (ACCPAYPAYMENT) and credit-note, prepayment and overpayment entries.
            if p.get("PaymentType", RECEIVED) != RECEIVED or invoice.get("Type", SALES) != SALES:
                continue
            paid_at = _xero_date(p["Date"])
            if paid_at <= since:
                continue
            out.append(
                Payment(
                    ref=ExternalRef(system="xero", id=p["PaymentID"]),
                    amount=Decimal(str(p["Amount"])),
                    currency=invoice.get("CurrencyCode") or "NZD",
                    paid_at=paid_at,
                    invoice_ref=(
                        ExternalRef(system="xero", id=invoice["InvoiceID"])
                        if invoice.get("InvoiceID")
                        else None
                    ),
                )
            )
        return out

    async def bank_balance(self) -> tuple[Decimal, str]:
        """Sum of closing balances from Xero's Bank Summary report, in the base currency."""
        org = (await self._get("/Organisation")).get("Organisations") or [{}]
        currency = str(org[0].get("BaseCurrency") or "NZD")
        report = (await self._get("/Reports/BankSummary")).get("Reports") or [{}]
        total = Decimal(0)
        for section in report[0].get("Rows") or []:
            for row in section.get("Rows") or []:
                if row.get("RowType") != "Row":
                    continue  # SummaryRow repeats the total
                cells = row.get("Cells") or []
                if cells:
                    total += Decimal(str(cells[-1].get("Value") or 0))
        return total, currency

    # -- draft creation, exactly once
    def _ledger_key(self, kind: str, draft: InvoiceDraft | BillDraft) -> str:
        raw = draft.idempotency_key or f"ref:{draft.reference}"
        key = f"{kind}:{raw}"
        return key if len(key) <= 150 else f"{kind}:{hashlib.sha256(raw.encode()).hexdigest()}"

    async def _create(
        self, kind: str, draft: InvoiceDraft | BillDraft, account: str
    ) -> PostedDocument:
        key = self._ledger_key(kind, draft)
        prior = self.ledger.get(key)
        if prior is None and self.ledger.claim(key):
            return await self._post(kind, key, draft, account)
        prior = self.ledger.get(key)
        if prior is not None and prior["status"] == "sent":
            return await self._read(str(prior["provider_id"]))
        if prior is not None and datetime.now(UTC) - prior["at"] < IN_FLIGHT:
            raise XeroUncertain("a create for this key is in flight")
        # Crashed mid-call: reconcile against Xero before any retry (same Idempotency-Key).
        existing = await self._find(kind, draft.reference)
        if existing is not None:
            self.ledger.put(key, existing.id)
            return existing
        return await self._post(kind, key, draft, account)

    async def _read(self, invoice_id: str) -> PostedDocument:
        """A document we already created, with Xero's figures as they are now.

        If Xero can't be reached the id is still returned, without figures: a repeat of a
        finished create must not fail just because the lookup did.
        """
        try:
            rows = (await self._get(f"/Invoices/{invoice_id}")).get("Invoices") or []
        except (XeroFailed, httpx.HTTPError):
            rows = []
        return _posted(rows[0]) if rows else PostedDocument(system="xero", id=invoice_id)

    async def _find(self, kind: str, reference: str) -> PostedDocument | None:
        field = "Reference" if kind == "ACCREC" else "InvoiceNumber"
        where = f"Type=={_quote(kind)} AND {field}=={_quote(reference)}"
        found = (await self._get("/Invoices", params={"where": where})).get("Invoices") or []
        live = [i for i in found if i.get("Status") not in ("DELETED", "VOIDED")]
        return _posted(live[0]) if live else None

    async def _post(
        self, kind: str, key: str, draft: InvoiceDraft | BillDraft, account: str
    ) -> PostedDocument:
        contact = await self.upsert_contact(draft.party)
        body: dict[str, Any] = {
            "Type": kind,
            "Contact": {"ContactID": contact.id},
            "Status": "DRAFT",
            "LineAmountTypes": "Exclusive",
            "CurrencyCode": draft.currency,
            "LineItems": [
                {
                    "Description": line.description,
                    "Quantity": str(line.quantity),
                    "UnitAmount": str(line.unit_amount),
                    "AccountCode": account,
                }
                for line in draft.lines
            ],
        }
        body["Reference" if kind == "ACCREC" else "InvoiceNumber"] = draft.reference
        if draft.due is not None:
            body["DueDate"] = draft.due.date().isoformat()
        try:
            resp = await self._api(
                "POST",
                "/Invoices",
                json={"Invoices": [body]},
                params={"summarizeErrors": "false"},
                extra={"Idempotency-Key": key},
            )
        except httpx.ConnectError as exc:  # never connected: nothing created
            self.ledger.release(key)
            raise XeroFailed(str(exc)) from exc
        except httpx.HTTPError as exc:  # may have reached Xero: stays "sending"
            raise XeroUncertain(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 500:
            raise XeroUncertain(f"{resp.status_code}: {resp.text[:200]}")
        try:
            created = resp.json()["Invoices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if resp.status_code >= 400:
                self.ledger.release(key)
                raise XeroFailed(f"{resp.status_code}: {resp.text[:200]}") from exc
            raise XeroUncertain("unreadable Xero response") from exc
        if resp.status_code >= 400 or created.get("HasErrors"):
            self.ledger.release(key)
            errors = [e.get("Message") for e in created.get("ValidationErrors") or []]
            raise XeroFailed(f"{resp.status_code}: {errors or resp.text[:200]}")
        self.ledger.put(key, str(created["InvoiceID"]))
        return _posted(created)


def create() -> XeroAccounting:
    """Build from BIZ_XERO_CLIENT_ID / BIZ_XERO_CLIENT_SECRET (a Xero custom connection).

    Optional: BIZ_XERO_TENANT_ID, BIZ_XERO_SALES_ACCOUNT (default 200),
    BIZ_XERO_PURCHASES_ACCOUNT (default 300).
    """
    return XeroAccounting(
        os.environ["BIZ_XERO_CLIENT_ID"],
        os.environ["BIZ_XERO_CLIENT_SECRET"],
        tenant_id=os.environ.get("BIZ_XERO_TENANT_ID") or None,
        sales_account=os.environ.get("BIZ_XERO_SALES_ACCOUNT", "200"),
        purchases_account=os.environ.get("BIZ_XERO_PURCHASES_ACCOUNT", "300"),
        ledger=SqlLedger(_db_engine()),
    )
