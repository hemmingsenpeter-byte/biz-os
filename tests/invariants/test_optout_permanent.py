"""An opt-out blocks every later send to that address, on every channel, including queued ones."""

from __future__ import annotations

import pytest
from biz_cap_comms import config, handle_packet
from biz_cap_comms.gate import GateConfig
from biz_cap_comms.tools import register
from biz_kernel.records.models import Party, Person
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeMail, FakeTelephony


@pytest.fixture
def env(session):  # type: ignore[no-untyped-def]
    config.configure(GateConfig())
    mail, tel = FakeMail(), FakeTelephony()
    reg = ToolRegistry()
    register(reg, {"mail": mail, "telephony": tel})
    yield session, reg, mail, tel
    config.configure(None)


def _msg(reg, session, key, channel, to, party_id=None):  # type: ignore[no-untyped-def]
    args = {"channel": channel, "to": to, "body": "Hello", "idempotency_key": key}
    if party_id:
        args["party_id"] = party_id
    d = reg.get("comms.draft").fn(session=session, **args)
    q = reg.get("comms.queue").fn(session=session, message_id=d["message_id"])
    return d["message_id"], q["status"]


def _send(reg, session, mid, key):  # type: ignore[no-untyped-def]
    return reg.get("comms.send").fn(session=session, message_id=mid, idempotency_key=key)


def test_message_queued_before_optout_is_blocked_at_send(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, _ = env
    mid, status = _msg(reg, session, "k1", "mail", "Ann@X.org")
    assert status == "queued"
    reg.get("comms.opt_out").fn(session=session, address="ann@x.org")
    out = _send(reg, session, mid, "k1")
    assert out["status"] == "blocked" and "suppressed" in out["reasons"]
    assert mail.sent == []
    # permanent: re-queueing, new drafts, and replays stay blocked
    assert reg.get("comms.queue").fn(session=session, message_id=mid)["status"] == "blocked"
    assert _msg(reg, session, "k2", "mail", "ann@x.org")[1] == "blocked"


def test_optout_blocks_every_channel_for_the_party(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, tel = env
    party = Party(kind="person", name="Ann")
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name="Ann", email="ann@x.org", phone="+64 21 0"))
    session.flush()
    pid = str(party.id)
    mid, status = _msg(reg, session, "k1", "sms", "+64210", pid)
    assert status == "queued"
    # an email stop reply from a party opts out SMS too
    out = reg.get("comms.classify_reply").fn(
        session=session, channel="mail", sender="ann@x.org", body="STOP", party_id=pid
    )
    assert out["reason"] == "opt_out"
    assert _send(reg, session, mid, "k1")["status"] == "blocked"
    assert _msg(reg, session, "k2", "sms", "+64 21-0", None)[1] == "blocked"  # linked number
    assert _msg(reg, session, "k3", "mail", "ann@x.org")[1] == "blocked"
    assert mail.sent == [] and tel.sent == []


def test_stop_reply_is_recorded_before_any_model(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, _, _ = env
    out = reg.get("comms.classify_reply").fn(
        session=session, channel="sms", sender="+64211", body="stop."
    )
    assert out["needs_model"] is False
    assert _msg(reg, session, "k1", "sms", "+64211")[1] == "blocked"
    ok = reg.get("comms.classify_reply").fn(
        session=session, channel="mail", sender="b@x.org", body="Can you help?"
    )
    assert ok["needs_model"] is True
    assert _msg(reg, session, "k2", "mail", "b@x.org")[1] == "queued"


def test_packet_consumer_records_stop(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, _, _ = env
    assert handle_packet(
        session, "reply.received", {"channel": "mail", "sender": "c@x.org", "body": "unsubscribe"}
    )
    assert not handle_packet(
        session, "reply.received", {"channel": "mail", "sender": "d@x.org", "body": "thanks"}
    )
    assert _msg(reg, session, "k1", "mail", "c@x.org")[1] == "blocked"
