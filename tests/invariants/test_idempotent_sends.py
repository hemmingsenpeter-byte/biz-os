"""A send never goes out twice for one key, even across a crash; uncertain waits for reconcile."""

from __future__ import annotations

import pytest
from biz_cap_comms import config
from biz_cap_comms.gate import GateConfig
from biz_cap_comms.tools import register
from biz_kernel.contracts.ports import SendResult
from biz_kernel.health.models import Alert
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeMail, FakeTelephony
from sqlalchemy import select


@pytest.fixture
def env(session):  # type: ignore[no-untyped-def]
    config.configure(GateConfig())
    mail, tel = FakeMail(), FakeTelephony()
    reg = ToolRegistry()
    register(reg, {"mail": mail, "telephony": tel})
    yield session, reg, mail, tel
    config.configure(None)


def _queued(reg, session, key="k1", channel="mail", to="a@x.org"):  # type: ignore[no-untyped-def]
    d = reg.get("comms.draft").fn(
        session=session, channel=channel, to=to, subject="Hi", body="Hello", idempotency_key=key
    )
    q = reg.get("comms.queue").fn(session=session, message_id=d["message_id"])
    assert q["status"] == "queued"
    return d["message_id"]


def _send(reg, session, mid, key="k1"):  # type: ignore[no-untyped-def]
    return reg.get("comms.send").fn(session=session, message_id=mid, idempotency_key=key)


def test_every_external_tool_declares_idempotency_key(env) -> None:  # type: ignore[no-untyped-def]
    _, reg, _, _ = env
    assert reg.not_idempotent() == []
    assert "idempotency_key" in reg.get("comms.send").schema["properties"]


def test_same_key_sends_once(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, _ = env
    mid = _queued(reg, session)
    first = _send(reg, session, mid)
    again = _send(reg, session, mid)
    assert first["status"] == again["status"] == "sent"
    assert first["provider_id"] == again["provider_id"]
    assert len(mail.sent) == 1
    # drafting twice with the same key yields the same message
    d2 = reg.get("comms.draft").fn(
        session=session, channel="mail", to="a@x.org", body="Hello", idempotency_key="k1"
    )
    assert d2["message_id"] == mid


def test_crash_between_provider_send_and_commit(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, _ = env
    mid = _queued(reg, session, channel="sms", to="+64210")
    nested = session.begin_nested()
    assert _send(reg, session, mid)["status"] == "sent"
    nested.rollback()  # the DB write is lost; the provider's ledger remembers the key
    session.expire_all()
    out = _send(reg, session, mid)
    assert out["status"] == "sent"
    assert len(_tel(env).sent) == 1


def _tel(env):  # type: ignore[no-untyped-def]
    return env[3]


def test_uncertain_is_not_retried_until_reconciled(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, _ = env
    mid = _queued(reg, session)
    mail.fail_next = "uncertain"
    assert _send(reg, session, mid)["status"] == "uncertain"
    assert session.scalars(select(Alert).where(Alert.kind == "comms.uncertain_send")).one()
    n = len(mail.sent)
    for _ in range(3):
        assert _send(reg, session, mid)["status"] == "uncertain"
    assert len(mail.sent) == n  # never retried
    # no adapter reconcile: stays uncertain
    assert reg.get("comms.reconcile").fn(session=session, message_id=mid)["status"] == "uncertain"
    assert _send(reg, session, mid)["status"] == "uncertain"
    # the provider now says it went out
    mail.reconcile = lambda key: SendResult(status="sent", provider_id="p-1")  # type: ignore[attr-defined]
    out = reg.get("comms.reconcile").fn(session=session, message_id=mid)
    assert out["status"] == "sent" and out["provider_id"] == "p-1"
    assert _send(reg, session, mid)["status"] == "sent"
    assert len(mail.sent) == n


def test_reconcile_confirmed_failure_allows_requeue(env) -> None:  # type: ignore[no-untyped-def]
    session, reg, mail, _ = env
    mid = _queued(reg, session)
    mail.fail_next = "uncertain"
    _send(reg, session, mid)
    mail.reconcile = lambda key: SendResult(status="failed", error="never sent")  # type: ignore[attr-defined]
    assert reg.get("comms.reconcile").fn(session=session, message_id=mid)["status"] == "failed"
    assert reg.get("comms.queue").fn(session=session, message_id=mid)["status"] == "queued"
