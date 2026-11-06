"""DocuSealSign against a mocked DocuSeal REST API."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from biz_kernel.contracts import suite
from biz_kernel.contracts.ports import Signer, SignPort
from biz_port_sign_docuseal import (
    DocuSealSign,
    SignRequestFailed,
    SignRequestUncertain,
    WebhookRejected,
    create,
)

FORM_COMPLETED = {
    "event_type": "form.completed",
    "timestamp": "2030-01-02T03:04:05Z",
    "data": {"id": 11, "email": "a@example.com", "submission": {"id": 77}},
}


class FakeDocuSeal:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.mode = "ok"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["x-auth-token"] == "key"
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if self.mode == "connect":
            raise httpx.ConnectTimeout("no route", request=req)
        if self.mode == "error":
            return httpx.Response(422, json={"error": "bad"})
        n = len(self.requests)
        if req.url.path == "/api/submissions/pdf":
            return httpx.Response(
                200, json={"id": 100 + n, "submitters": [{"slug": f"s{n}", "email": "a@x.org"}]}
            )
        return httpx.Response(200, json=[{"submission_id": 100 + n, "embed_src": "https://d/s/x"}])


def _port(api: FakeDocuSeal, **kw: Any) -> DocuSealSign:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return DocuSealSign("https://docuseal.test/", "key", http=http, **kw)


SIGNERS = [Signer(name="A", email="a@example.com")]


def test_is_a_sign_port_and_passes_contract_suite() -> None:
    port = _port(FakeDocuSeal(), template_id=5, allow_template_mode=True)
    assert isinstance(port, SignPort)
    asyncio.run(suite.check_sign_port(port, FORM_COMPLETED))


def test_template_submission_request_shape() -> None:
    api = FakeDocuSeal()
    ref = asyncio.run(
        _port(api, template_id=5, allow_template_mode=True).request_signature(uuid.uuid4(), SIGNERS)
    )
    assert (ref.system, ref.id, ref.url) == ("docuseal", "101", "https://d/s/x")
    req = api.requests[0]
    assert str(req.url) == "https://docuseal.test/api/submissions"
    body = json.loads(req.content)
    assert body["template_id"] == 5
    assert body["submitters"] == [{"name": "A", "email": "a@example.com", "role": "signer"}]


def test_pdf_submission_uploads_document() -> None:
    api = FakeDocuSeal()
    port = _port(api, document_loader=lambda _id: ("Contract", b"%PDF-1"))
    ref = asyncio.run(port.request_signature(uuid.uuid4(), SIGNERS))
    assert ref.id == "101"
    body = json.loads(api.requests[0].content)
    assert api.requests[0].url.path == "/api/submissions/pdf"
    assert body["documents"][0]["name"] == "Contract" and body["documents"][0]["file"]


def test_ledger_is_keyed_on_idempotency_key() -> None:
    api = FakeDocuSeal()
    port = _port(api, template_id=5, allow_template_mode=True)
    doc = uuid.uuid4()
    a = asyncio.run(port.request_signature(doc, SIGNERS, idempotency_key="k1"))
    assert asyncio.run(port.request_signature(doc, SIGNERS, idempotency_key="k1")) == a
    b = asyncio.run(port.request_signature(doc, SIGNERS, idempotency_key="k2"))
    assert b.id != a.id and len(api.requests) == 2


def test_template_mode_needs_explicit_enable() -> None:
    api = FakeDocuSeal()
    with pytest.raises(SignRequestFailed):
        asyncio.run(_port(api, template_id=5).request_signature(uuid.uuid4(), SIGNERS))
    assert api.requests == []


def test_same_document_and_signers_creates_one_submission() -> None:
    api = FakeDocuSeal()
    port = _port(api, template_id=5, allow_template_mode=True)
    doc = uuid.uuid4()
    a = asyncio.run(port.request_signature(doc, SIGNERS))
    b = asyncio.run(port.request_signature(doc, SIGNERS))
    assert a == b and len(api.requests) == 1
    asyncio.run(port.request_signature(doc, [Signer(name="B", email="b@example.com")]))
    assert len(api.requests) == 2


def test_error_is_retryable_and_timeout_is_uncertain() -> None:
    api = FakeDocuSeal()
    port = _port(api, template_id=5, allow_template_mode=True)
    doc = uuid.uuid4()
    api.mode = "error"
    with pytest.raises(SignRequestFailed):
        asyncio.run(port.request_signature(doc, SIGNERS))
    api.mode = "connect"
    with pytest.raises(SignRequestFailed):
        asyncio.run(port.request_signature(doc, SIGNERS))
    api.mode = "timeout"
    with pytest.raises(SignRequestUncertain):
        asyncio.run(port.request_signature(doc, SIGNERS))
    api.mode = "ok"  # the request may have gone out: no automatic retry
    with pytest.raises(SignRequestUncertain):
        asyncio.run(port.request_signature(doc, SIGNERS))
    assert len(api.requests) == 3


def test_no_template_or_loader_fails_without_calling() -> None:
    api = FakeDocuSeal()
    with pytest.raises(SignRequestFailed):
        asyncio.run(_port(api).request_signature(uuid.uuid4(), SIGNERS))
    assert api.requests == []


@pytest.mark.parametrize(
    ("event_type", "data", "kind", "request_id", "email"),
    [
        ("form.completed", {"email": "a@x.org", "submission": {"id": 7}}, "signed", "7", "a@x.org"),
        ("form.viewed", {"email": "a@x.org", "submission_id": 8}, "viewed", "8", "a@x.org"),
        (
            "form.declined",
            {"email": "a@x.org", "submission": {"id": 9}},
            "declined",
            "9",
            "a@x.org",
        ),
        ("submission.completed", {"id": 10}, "completed", "10", None),
        ("template.created", {"id": 1, "submission_id": 11}, "other", "11", None),
    ],
)
def test_parse_webhook(event_type: str, data: dict, kind: str, request_id: str, email) -> None:  # type: ignore[no-untyped-def]
    port = _port(FakeDocuSeal())
    ev = port.parse_webhook({"form": {"event_type": event_type, "data": data}, "headers": {}})
    assert (ev.kind, ev.request_id, ev.signer_email) == (kind, request_id, email)


def test_parse_webhook_rejects_malformed() -> None:
    port = _port(FakeDocuSeal())
    for bad in ({"form": {"nope": 1}}, {"form": {"event_type": "form.completed", "data": {}}}, 5):
        with pytest.raises(WebhookRejected):
            port.parse_webhook(bad)


def test_create_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIZ_DOCUSEAL_URL", "https://docuseal.test")
    monkeypatch.setenv("BIZ_DOCUSEAL_API_KEY", "key")
    monkeypatch.setenv("BIZ_DOCUSEAL_TEMPLATE_ID", "3")
    monkeypatch.delenv("BIZ_STORAGE_ROOT", raising=False)
    port = create()
    assert port.base_url == "https://docuseal.test" and port.template_id == 3
