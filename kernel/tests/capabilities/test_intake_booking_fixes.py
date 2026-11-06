"""Regression tests for the M2 review: identity, opt-outs, text-back, double booking, limiter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from biz_cap_booking.tools import SlotUnavailable
from biz_cap_booking.tools import register as reg_booking
from biz_cap_intake import config, service
from biz_cap_intake.models import Cursor, Enquiry, SentText
from biz_cap_intake.web import RateLimiter, client_ip, poll_mail
from biz_kernel import db
from biz_kernel.contracts.ports import InboundMessage
from biz_kernel.records.models import Party, Person
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeCalendar, FakeTelephony
from sqlalchemy import func, select


class StubBus:
    def __init__(self) -> None:
        self.out: list[dict] = []

    def publish(self, session, **kw):  # type: ignore[no-untyped-def]
        self.out.append(kw)
        return SimpleNamespace(id="p", **kw)


@pytest.fixture(autouse=True)
def bound():  # type: ignore[no-untyped-def]
    config.bind(SimpleNamespace(policy={"intake": {"default_country_code": "44"}}, slot_fills={}))
    yield
    config.bind(None)


def test_sms_never_triggers_text_back(session) -> None:  # type: ignore[no-untyped-def]
    bus = StubBus()
    for i in (1, 2):
        service.receive(
            session, bus, channel="sms", sender="+447700900001", body="thanks", provider_id=f"S{i}"
        )
    assert [p["payload"]["text_back"] for p in bus.out] == ["no", "no"]


def test_missed_call_text_back_once_per_number_per_day(session) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_intake.tools import Suppressed, register

    config.bind(
        SimpleNamespace(
            policy={"intake": {"text_back_template": "Hi. STOP", "default_country_code": "44"}},
            slot_fills={},
        )
    )
    phone = FakeTelephony()
    reg = ToolRegistry()
    register(reg, {"telephony": phone, "mail": None})
    bus = StubBus()
    service.receive(session, bus, channel="phone", sender="+447700900002", provider_id="C1")
    assert bus.out[-1]["payload"]["text_back"] == "yes"
    reg.get("intake.text_back").fn(session=session, to="+447700900002", idempotency_key="k1")
    service.receive(session, bus, channel="phone", sender="+44 7700 900002", provider_id="C2")
    assert bus.out[-1]["payload"]["text_back"] == "no"
    with pytest.raises(Suppressed):
        reg.get("intake.text_back").fn(session=session, to="07700 900002", idempotency_key="k2")
    assert len(phone.sent) == 1
    old = session.get(SentText, "k1")
    old.at = datetime.now(UTC) - timedelta(hours=25)
    session.flush()
    service.receive(session, bus, channel="phone", sender="+447700900002", provider_id="C3")
    assert bus.out[-1]["payload"]["text_back"] == "yes"


def test_optout_survives_number_formatting(session) -> None:  # type: ignore[no-untyped-def]
    service.add_opt_out(session, "+447700900123", "sms_stop")
    assert service.is_opted_out(session, "07700 900123")
    assert service.is_opted_out(session, "+44 7700 900123")
    assert service.is_opted_out(session, "+44 (7700) 900-123")
    service.add_opt_out(session, "  Ann@Example.COM ", "mail")
    assert service.is_opted_out(session, "ann@example.com")


def test_no_default_country_code_leaves_digits(session) -> None:  # type: ignore[no-untyped-def]
    config.bind(SimpleNamespace(policy={}, slot_fills={}))
    assert service.normalize_address("07700 900123") == "07700900123"


@pytest.mark.parametrize(
    "text",
    [
        "Hi, please take me off the waiting list at the other firm, we'd like you to quote "
        "our 2,000 sq ft office fit-out.",
        "take me off the other waiting list, please quote",
        "please stop by tomorrow to look at the office",
    ],
)
def test_long_messages_are_not_opt_outs(text) -> None:  # type: ignore[no-untyped-def]
    assert not service.is_opt_out_text(text)
    assert service.classify_rules("mail", "a@b.com", "Quote please", text * 2)["reason"] is None


@pytest.mark.parametrize("text", ["STOP", "stop.", "Unsubscribe", "please unsubscribe", "stop all"])
def test_short_stop_messages_opt_out(text) -> None:  # type: ignore[no-untyped-def]
    assert service.is_opt_out_text(text)


def test_form_claim_never_links_existing_party(session) -> None:  # type: ignore[no-untyped-def]
    party = Party(kind="person", name="Real Client")
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name="Real Client", email="ceo@client.com"))
    session.flush()
    got = service.receive(
        session,
        StubBus(),
        channel="form",
        sender="CEO@client.com",
        body="urgent: change bank",
        provider_id="form:x",
    )
    pid = service.ensure_party(session, got.enquiry)
    assert pid != party.id
    assert got.enquiry.verified is False and got.enquiry.claimed_email == "ceo@client.com"
    assert "unverified contact" in got.packet.summary
    new = session.get(Person, session.scalar(select(Person.id).where(Person.party_id == pid)))
    assert new.email is None and new.phone is None


def test_double_booking_refused(session) -> None:  # type: ignore[no-untyped-def]
    reg = ToolRegistry()
    cal = FakeCalendar()
    reg_booking(reg, {"calendar": cal})
    offer, book = reg.tools["booking.offer_slots"].fn, reg.tools["booking.book"].fn
    nb = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    a = offer(session, not_before=nb)["slots"][0]
    b = offer(session, not_before=nb)["slots"][0]
    book(session, start=a["start"], end=a["end"], title="A", idempotency_key="run1:book")
    with pytest.raises(SlotUnavailable):
        book(session, start=b["start"], end=b["end"], title="B", idempotency_key="run2:book")
    assert len(cal.events) == 1
    # a replay of the first key is still idempotent
    again = book(session, start=a["start"], end=a["end"], title="A", idempotency_key="run1:book")
    assert again["meeting_id"] and len(cal.events) == 1


def test_booking_in_the_past_refused(session) -> None:  # type: ignore[no-untyped-def]
    reg = ToolRegistry()
    reg_booking(reg, {"calendar": FakeCalendar()})
    with pytest.raises(SlotUnavailable):
        reg.tools["booking.book"].fn(
            session,
            start="2020-01-07T10:00:00+00:00",
            end="2020-01-07T10:30:00+00:00",
            title="x",
            idempotency_key="past",
        )


def test_rate_limiter_is_bounded() -> None:
    lim = RateLimiter(limit=2, window=10, max_keys=5)
    for i in range(50):
        lim.allow(f"ip{i}", now=float(i) / 100)
    assert len(lim._hits) <= 5
    assert lim.allow("fresh", now=100.0)


def _req(peer: str, **headers: str):  # type: ignore[no-untyped-def]
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_ip_trusts_headers_only_from_trusted_proxy() -> None:
    import ipaddress

    trusted = [ipaddress.ip_network("10.0.0.0/8")]
    assert client_ip(_req("10.0.0.5", **{"cf-connecting-ip": "203.0.113.9"}), trusted) == (
        "203.0.113.9"
    )
    xff = {"x-forwarded-for": "6.6.6.6, 203.0.113.7"}
    assert client_ip(_req("10.0.0.5", **xff), trusted) == "203.0.113.7"
    # an untrusted direct peer cannot spoof the headers
    assert client_ip(_req("198.51.100.1", **{"cf-connecting-ip": "1.2.3.4"}), trusted) == (
        "198.51.100.1"
    )


def test_poll_mail_isolates_bad_message_and_advances_cursor(fresh_db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from biz_kernel.models_registry import import_all_models
    from sqlalchemy import create_engine

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    now = datetime.now(UTC)

    def msg(pid, minutes, body="hello"):  # type: ignore[no-untyped-def]
        return InboundMessage(
            provider_id=pid,
            sender="a@b.com",
            to=["x@y.com"],
            subject="s",
            body=body,
            received_at=now - timedelta(minutes=minutes),
        )

    msgs = [msg("m1", 3), msg("m2", 2, "boom"), msg("m3", 1, "x" * 100_000)]

    class Mail:
        async def fetch_new(self, since):  # type: ignore[no-untyped-def]
            return msgs

    real = service.receive

    def flaky(session, bus, **kw):  # type: ignore[no-untyped-def]
        if kw["body"] == "boom":
            raise RuntimeError("bad row")
        return real(session, bus, **kw)

    monkeypatch.setattr(service, "receive", flaky)
    bus = StubBus()
    rt = SimpleNamespace(ports={"mail": Mail()}, bus=bus)
    assert poll_mail(rt) == 2
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Enquiry)) == 2
        longest = max(len(b) for b in s.scalars(select(Enquiry.body)))
        assert longest <= service.MAX_BODY
        assert s.get(Cursor, "mail").value >= (now - timedelta(minutes=1)).replace(microsecond=0)
    eng.dispose()
