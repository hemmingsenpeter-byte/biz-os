"""An in-memory Xero for httpx.MockTransport: token, Contacts, Invoices, Payments.

It honours Idempotency-Key like Xero does, and records every request in `requests`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx


class FakeXero:
    def __init__(self) -> None:
        self.contacts: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.by_key: dict[str, dict[str, Any]] = {}
        self.fail_next_post: Exception | None = None  # raised after the invoice is stored
        self.reject_next_post: str | None = None  # a validation error message

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "identity.xero.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401)
        where = request.url.params.get("where", "")
        if path.endswith("/Contacts"):
            if request.method == "GET":
                return httpx.Response(200, json={"Contacts": _match(self.contacts, where)})
            body = json.loads(request.content)["Contacts"][0]
            row = {**body, "ContactID": str(uuid.uuid4())}
            self.contacts.append(row)
            return httpx.Response(200, json={"Contacts": [row]})
        if path.endswith("/Invoices"):
            if request.method == "GET":
                return httpx.Response(200, json={"Invoices": _match(self.invoices, where)})
            key = request.headers.get("Idempotency-Key", "")
            if key in self.by_key:
                return httpx.Response(200, json={"Invoices": [self.by_key[key]]})
            body = json.loads(request.content)["Invoices"][0]
            if self.reject_next_post:
                msg, self.reject_next_post = self.reject_next_post, None
                err = {**body, "HasErrors": True, "ValidationErrors": [{"Message": msg}]}
                return httpx.Response(400, json={"Invoices": [err]})
            row = {**body, "InvoiceID": str(uuid.uuid4()), "HasErrors": False}
            self.invoices.append(row)
            if key:
                self.by_key[key] = row
            if self.fail_next_post is not None:
                exc, self.fail_next_post = self.fail_next_post, None
                raise exc
            return httpx.Response(200, json={"Invoices": [row]})
        if path.endswith("/Payments"):
            return httpx.Response(200, json={"Payments": self.payments})
        return httpx.Response(404)


def _match(rows: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """Enough of Xero's `where` for the adapter: Field=="value" joined by AND."""
    conds = re.findall(r'(\w+)=="((?:[^"\\]|\\.)*)"', where)
    return [r for r in rows if all(str(r.get(f)) == v for f, v in conds)]
