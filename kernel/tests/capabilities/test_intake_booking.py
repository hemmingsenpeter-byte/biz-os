"""Unit tests: SOP conditions, intake rules and routes, booking tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from biz_cap_booking.tools import register as register_booking
from biz_cap_intake import config, service
from biz_cap_intake.models import Enquiry
from biz_cap_intake.web import RateLimiter, build_router
from biz_kernel.api.app import create_app, mount_capability
from biz_kernel.auth.deps import get_session
from biz_kernel.packets.bus import Bus, Packet, SchemaRegistry
from biz_kernel.tools.registry import ToolRegistry
from biz_kernel.workflows.templating import TemplateError, holds
from biz_port_fakes import FakeCalendar, FakeTelephony
from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).parents[3]


def test_when_conditions() -> None:
    ctx = {"steps": {"a": {"output": {"category": "Spam", "flag": True}}}}
    assert holds("{{steps.a.output.category}} == spam", ctx)
    assert holds("{{steps.a.output.category}} != enquiry", ctx)
    assert holds("{{steps.a.output.category}} in spam,other", ctx)
    assert not holds("{{steps.a.output.category}} in enquiry,other", ctx)
    assert holds("{{steps.a.output.flag}} == true", ctx)
    with pytest.raises(TemplateError):
        holds("{{steps.b.output.x}} == 1", ctx)
    with pytest.raises(ValueError):
        holds("nonsense", ctx)


@pytest.mark.parametrize(
    ("channel", "sender", "subject", "body", "category", "reason"),
    [
        ("sms", "+64", "", "STOP", "other", "opt_out"),
        ("mail", "a@x.org", "Please unsubscribe", "bye", "other", "opt_out"),
        ("mail", "mailer-daemon@x.org", "Undeliverable", "x", "other", "auto_reply"),
        ("mail", "a@x.org", "Automatic reply: away", "x", "other", "auto_reply"),
        ("mail", "a@x.org", "SEO services", "guaranteed ranking", "spam", "spam_rule"),
        ("mail", "a@x.org", "Quote", "Can you quote an office?", None, None),
    ],
)
def test_classify_rules(channel, sender, subject, body, category, reason) -> None:  # type: ignore[no-untyped-def]
    out = service.classify_rules(channel, sender, subject, body)
    assert (out["category"], out["reason"]) == (category, reason)
    assert out["needs_model"] is (category is None)


@pytest.fixture
def rt(session):  # type: ignore[no-untyped-def]
    reg = SchemaRegistry()
    reg.load_dir(ROOT / "packets")
    return SimpleNamespace(bus=Bus(reg), ports={"telephony": FakeTelephony()})


@pytest.fixture
def client(rt, session):  # type: ignore[no-untyped-def]
    app = create_app()
    mount_capability(app, "intake", build_router(rt, RateLimiter(limit=3)))
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_webhook_dedupes_and_publishes_once(client, session) -> None:  # type: ignore[no-untyped-def]
    body = {"kind": "inbound_sms", "from": "+64210", "to": "+649", "body": "hi", "id": "SM1"}
    assert client.post("/v1/intake/telephony", json=body).status_code == 200
    assert client.post("/v1/intake/telephony", json=body).json()["status"] == "duplicate"
    packets = session.scalars(select(Packet).where(Packet.type == "enquiry.received")).all()
    assert len(packets) == 1 and packets[0].payload["text_back"] == "yes"
    assert client.post("/v1/intake/telephony", json={"kind": "other"}).json() == {
        "status": "ignored"
    }


def test_bad_signature_is_403(rt, session) -> None:  # type: ignore[no-untyped-def]
    class Rejecting:
        def parse_webhook(self, request):  # type: ignore[no-untyped-def]
            raise PermissionError("bad signature")

    rt.ports["telephony"] = Rejecting()
    app = create_app()
    mount_capability(app, "intake", build_router(rt))
    app.dependency_overrides[get_session] = lambda: session
    r = TestClient(app).post("/v1/intake/telephony", json={"kind": "missed_call"})
    assert r.status_code == 403
    assert session.scalars(select(Enquiry)).all() == []


def test_form_publishes_honeypot_and_rate_limit(client, session) -> None:  # type: ignore[no-untyped-def]
    form = {"name": "Ann", "email": "ann@example.com", "message": "Hello"}
    assert client.post("/v1/intake/form", json=form).status_code == 202
    assert client.post("/v1/intake/form", json=form).status_code == 202  # same message: deduped
    packets = session.scalars(select(Packet).where(Packet.type == "enquiry.received")).all()
    assert len(packets) == 1 and packets[0].payload["channel"] == "form"
    assert client.post("/v1/intake/form", json={**form, "website": "spam.io"}).status_code == 202
    assert client.post("/v1/intake/form", json=form).status_code == 429  # limit of 3 reached
    assert len(session.scalars(select(Packet).where(Packet.type == "enquiry.received")).all()) == 1


def test_text_back_uses_fixed_template(session, rt) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_intake.tools import register

    config.bind(
        SimpleNamespace(
            policy={"intake": {"text_back_template": "Sorry, call you soon."}}, slot_fills={}
        )
    )
    reg = ToolRegistry()
    register(reg, {"telephony": rt.ports["telephony"], "mail": None})
    tool = reg.get("intake.text_back")
    assert "idempotency_key" in tool.schema["properties"]
    args = {"to": "+64210", "idempotency_key": "k1"}
    a = tool.fn(session=session, **args)
    b = tool.fn(session=session, **args)
    assert a["body"] == "Sorry, call you soon." and a["message_id"] == b["message_id"]
    assert len(rt.ports["telephony"].sent) == 1


def test_booking_offer_and_idempotent_book(session) -> None:  # type: ignore[no-untyped-def]
    cal = FakeCalendar()
    reg = ToolRegistry()
    register_booking(reg, {"calendar": cal})
    offer = reg.get("booking.offer_slots").fn(session=session, days_ahead=7, limit=3)
    assert 1 <= len(offer["slots"]) <= 3
    slot = offer["slots"][0]
    assert datetime.fromisoformat(slot["start"]).tzinfo is UTC
    args = {"start": slot["start"], "end": slot["end"], "title": "Meet", "idempotency_key": "b1"}
    first = reg.get("booking.book").fn(session=session, **args)
    again = reg.get("booking.book").fn(session=session, **args)
    assert first["meeting_id"] == again["meeting_id"] and len(cal.events) == 1


def test_real_twilio_adapter_through_intake_route(rt, session) -> None:  # type: ignore[no-untyped-def]
    """The intake route and the Twilio adapter agree on the webhook contract and URL."""
    from biz_port_telephony_twilio import TwilioTelephony
    from biz_port_telephony_twilio.testing import FakeTwilioClient
    from twilio.request_validator import RequestValidator

    public = "https://hooks.example.com/v1/intake/telephony"
    rt.ports["telephony"] = TwilioTelephony(
        "AC1", "secret-token", "+6490000000", client=FakeTwilioClient(), webhook_url=public
    )
    app = create_app()
    mount_capability(app, "intake", build_router(rt))
    app.dependency_overrides[get_session] = lambda: session
    c = TestClient(app)
    form = {"CallSid": "CA1", "CallStatus": "no-answer", "From": "+64211", "To": "+6490000000"}
    good = RequestValidator("secret-token").compute_signature(public, form)
    ok = c.post("/v1/intake/telephony", data=form, headers={"X-Twilio-Signature": good})
    assert ok.status_code == 200, ok.text
    forged = c.post(
        "/v1/intake/telephony",
        data={**form, "CallSid": "CA2"},
        headers={"X-Twilio-Signature": good},
    )
    assert forged.status_code == 403
    packets = session.scalars(select(Packet).where(Packet.type == "enquiry.received")).all()
    assert len(packets) == 1 and packets[0].payload["channel"] == "phone"
