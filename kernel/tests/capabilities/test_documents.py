"""documents: template rendering, versioning, PDF, signature requests and webhooks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from biz_cap_documents import config, render
from biz_cap_documents.models import Signature
from biz_cap_documents.tools import register
from biz_cap_documents.web import apply_event, build_router
from biz_kernel.api.app import create_app
from biz_kernel.auth.deps import get_session
from biz_kernel.packets.bus import Bus, Packet, SchemaRegistry
from biz_kernel.records.models import Document
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeSign, FakeStorage
from biz_port_sign_docuseal import DocuSealSign
from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).parents[3]
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BIZ_DOCUSEAL_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("BIZ_DOCUSEAL_WEBHOOK_HEADER", raising=False)
    config.reset()
    yield
    config.reset()


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    (tmp_path / "scope.md").write_text(
        "# Scope for {{ client }}\n\n"
        "Total: **{{ total }}**\n\n"
        "- {{ items[0] }}\n- {{ items[1] }}\n\n"
        "| Item | Price |\n|---|---|\n| Walls | 10 |\n"
    )
    (tmp_path / "page.html").write_text("<html><body><p>Hello {{ name }}</p></body></html>")
    config.bind(
        SimpleNamespace(slot_fills={"industry.scope_template": ("pack", tmp_path / "scope.md")})
    )
    return tmp_path


def test_markdown_template_renders_and_escapes(templates: Path) -> None:
    out = render.render_html(
        templates / "scope.md",
        {"client": "<b>Bob & Co</b>", "total": "$5", "items": ["a", "b"]},
        "Scope",
    )
    assert "<h1>Scope for &lt;b&gt;Bob &amp; Co&lt;/b&gt;</h1>" in out
    assert "<strong>$5</strong>" in out
    assert "<ul><li>a</li><li>b</li></ul>" in out
    assert "<th>Item</th>" in out and "<td>Walls</td>" in out


def test_html_template_is_autoescaped(templates: Path) -> None:
    out = render.render_html(templates / "page.html", {"name": "<script>x</script>"})
    assert "&lt;script&gt;" in out and "<script>" not in out


def test_missing_field_fails_loudly(templates: Path) -> None:
    with pytest.raises(render.TemplateFailed, match="client"):
        render.render_html(templates / "scope.md", {"total": 1, "items": ["a", "b"]})


def test_template_is_sandboxed(tmp_path: Path) -> None:
    (tmp_path / "evil.html").write_text("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises(render.TemplateFailed):
        render.render_html(tmp_path / "evil.html", {})


def test_template_resolution_confined_to_roots(templates: Path, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    assert config.resolve_template("industry.scope_template") == (templates / "scope.md").resolve()
    assert config.resolve_template("page.html") == (templates / "page.html").resolve()
    outside = tmp_path_factory.mktemp("o") / "x.md"
    outside.write_text("x")
    for ref in (str(outside), "../x.md", "missing.md"):
        with pytest.raises(LookupError):
            config.resolve_template(ref)


@pytest.fixture
def tools(templates: Path) -> Any:
    reg = ToolRegistry()
    storage, sign = FakeStorage(), FakeSign()
    register(reg, {"storage": storage, "sign": sign})
    return SimpleNamespace(reg=reg, storage=storage, sign=sign)


DATA = {"client": "Bob", "total": "$5", "items": ["a", "b"]}


def _render(t: Any, session: Any, **kw: Any) -> dict[str, Any]:
    args = {"template": "industry.scope_template", "title": "Scope", "format": "html", **kw}
    return t.reg.get("documents.render").fn(session=session, **args)


def test_render_versions_and_is_idempotent(tools: Any, session: Any) -> None:
    v1 = _render(tools, session, data=DATA)
    again = _render(tools, session, data=DATA)
    assert again["document_id"] == v1["document_id"] and again["deduplicated"] is True
    v2 = _render(tools, session, data={**DATA, "total": "$6"})
    assert v2["version"] == 2 and v2["supersedes_id"] == v1["document_id"]
    assert v1["version"] == 1 and v1["supersedes_id"] is None
    rows = session.scalars(select(Document)).all()
    assert len(rows) == 2
    assert tools.storage.get(v2["uri"]).startswith(b"<!doctype html>")
    assert v2["sha256"] != v1["sha256"]


def test_render_missing_data_raises(tools: Any, session: Any) -> None:
    with pytest.raises(ValueError, match="missing"):
        _render(tools, session, data={})


def test_file_attaches_stored_file(tools: Any, session: Any) -> None:
    stored = tools.storage.put(b"photo", "p.jpg", "image/jpeg")
    out = tools.reg.get("documents.file").fn(session=session, uri=stored.uri, title="Photo")
    assert out["version"] == 1 and out["sha256"] == stored.sha256
    with pytest.raises(LookupError):
        tools.reg.get("documents.file").fn(session=session, uri="fake://nope", title="X")


@pytest.mark.skipif(not render.pdf_available(), reason="WeasyPrint/Pango not installed")
def test_render_pdf(tools: Any, session: Any) -> None:
    out = _render(tools, session, data=DATA, format="pdf")
    assert tools.storage.get(out["uri"]).startswith(b"%PDF")
    assert _render(tools, session, data=DATA, format="pdf")["deduplicated"] is True


def test_pdf_unavailable_is_a_clear_error(
    tools: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: str) -> bytes:
        raise RuntimeError("PDF rendering unavailable")

    monkeypatch.setattr(render, "html_to_pdf", boom)
    with pytest.raises(RuntimeError, match="unavailable"):
        _render(tools, session, data=DATA, format="pdf")


def test_gated_tool_declares_idempotency_key(tools: Any) -> None:
    assert tools.reg.not_idempotent() == []
    assert "idempotency_key" in tools.reg.get("documents.request_signature").schema["properties"]


# --- signature round trip through the DocuSeal adapter and the webhook ----------------------


class Api:
    """Minimal DocuSeal: creates submission 501 and records the call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=[{"submission_id": 501, "embed_src": "https://d/s/x"}])


@pytest.fixture
def stack(session: Any, templates: Path) -> Any:
    api = Api()
    sign = DocuSealSign(
        "https://docuseal.test",
        "key",
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        template_id=1,
    )
    storage = FakeStorage()
    reg = SchemaRegistry()
    reg.load_dir(ROOT / "packets")
    rt = SimpleNamespace(
        bus=Bus(reg), ports={"sign": sign, "storage": storage}, manifest=config._manifest
    )
    registry = ToolRegistry()
    register(registry, rt.ports)
    fetched: list[str] = []

    def fetch(url: str) -> bytes | None:
        fetched.append(url)
        return b"%PDF-signed"

    app = create_app()
    app.include_router(build_router(rt, fetch), prefix="/webhooks")
    app.dependency_overrides[get_session] = lambda: session
    return SimpleNamespace(
        api=api, rt=rt, reg=registry, client=TestClient(app), fetched=fetched, storage=storage
    )


def _request(stack: Any, session: Any, key: str = "k1") -> tuple[dict[str, Any], dict[str, Any]]:
    stored = stack.storage.put(b"%PDF-contract", "c.pdf", "application/pdf")
    doc = stack.reg.get("documents.file").fn(session=session, uri=stored.uri, title="Contract")
    out = stack.reg.get("documents.request_signature").fn(
        session=session,
        document_id=doc["document_id"],
        signers=[{"name": "A", "email": "a@example.com"}],
        idempotency_key=key,
    )
    return doc, out


def _hook(client: TestClient, body: dict[str, Any], secret: str | None = SECRET) -> Any:
    headers = {"X-Docuseal-Secret": secret} if secret is not None else {}
    return client.post("/webhooks/documents/sign", json=body, headers=headers)


COMPLETED = {
    "event_type": "form.completed",
    "timestamp": "2030-01-02T03:04:05Z",
    "data": {
        "email": "a@example.com",
        "submission": {"id": 501},
        "documents": [{"name": "c", "url": "https://docuseal.test/file/c.pdf"}],
    },
}


def test_request_signature_records_row_and_is_idempotent(stack: Any, session: Any) -> None:
    doc, out = _request(stack, session)
    assert out["status"] == "requested" and out["request_id"] == "501"
    again = stack.reg.get("documents.request_signature").fn(
        session=session,
        document_id=doc["document_id"],
        signers=[{"name": "A", "email": "a@example.com"}],
        idempotency_key="k1",
    )
    assert again["signature_id"] == out["signature_id"] and stack.api.calls == 1
    assert len(session.scalars(select(Signature)).all()) == 1


def test_webhook_round_trip_signs_stores_pdf_and_publishes_once(stack: Any, session: Any) -> None:
    doc, out = _request(stack, session)
    r = _hook(stack.client, COMPLETED)
    assert r.status_code == 200 and r.json() == {"status": "signed"}
    sig = session.scalars(select(Signature)).one()
    assert sig.status == "signed" and sig.signed_document_id is not None
    signed = session.get(Document, sig.signed_document_id)
    assert signed.version == 2 and signed.supersedes_id is not None
    assert stack.storage.get(signed.uri) == b"%PDF-signed"
    packets = session.scalars(select(Packet).where(Packet.type == "document.signed")).all()
    assert len(packets) == 1 and packets[0].payload["document_id"] == doc["document_id"]
    # replay: no change, no second packet, no second fetch
    assert _hook(stack.client, COMPLETED).json() == {"status": "duplicate"}
    assert _hook(
        stack.client, {**COMPLETED, "event_type": "submission.completed", "data": {"id": 501}}
    ).json() == {"status": "duplicate"}
    assert len(session.scalars(select(Packet).where(Packet.type == "document.signed")).all()) == 1
    assert len(stack.fetched) == 1


def test_webhook_declined(stack: Any, session: Any) -> None:
    _request(stack, session)
    body = {
        "event_type": "form.declined",
        "data": {"email": "a@example.com", "submission": {"id": 501}},
    }
    assert _hook(stack.client, body).json() == {"status": "declined"}
    assert session.scalars(select(Signature)).one().status == "declined"
    assert session.scalars(select(Packet).where(Packet.type == "document.signed")).all() == []
    assert _hook(stack.client, COMPLETED).json() == {"status": "duplicate"}  # terminal


def test_webhook_rejects_bad_or_missing_secret(stack: Any, session: Any) -> None:
    _request(stack, session)
    assert _hook(stack.client, COMPLETED, "wrong").status_code == 403
    assert _hook(stack.client, COMPLETED, None).status_code == 403
    assert session.scalars(select(Signature)).one().status == "requested"


def test_webhook_rejects_everything_when_secret_unset(
    stack: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIZ_DOCUSEAL_WEBHOOK_SECRET")
    assert _hook(stack.client, COMPLETED, "").status_code == 403


def test_webhook_malformed_and_unknown(stack: Any, session: Any) -> None:
    assert _hook(stack.client, {"nope": 1}).status_code == 403
    assert _hook(stack.client, COMPLETED).json() == {"status": "unknown"}


def test_multiple_signers_complete_only_when_all_signed(stack: Any, session: Any) -> None:
    stored = stack.storage.put(b"%PDF", "c.pdf", "application/pdf")
    doc = stack.reg.get("documents.file").fn(session=session, uri=stored.uri, title="Two")
    stack.reg.get("documents.request_signature").fn(
        session=session,
        document_id=doc["document_id"],
        signers=[{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com"}],
        idempotency_key="two",
    )
    assert _hook(stack.client, COMPLETED).json() == {"status": "progress"}
    assert session.scalars(select(Signature)).one().status == "requested"
    second = {**COMPLETED, "data": {**COMPLETED["data"], "email": "b@example.com"}}
    assert _hook(stack.client, second).json() == {"status": "signed"}
    assert apply_event  # imported for direct use by callers
