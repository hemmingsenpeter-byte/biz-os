"""A send never goes out twice for one key, even across a crash; uncertain waits for reconcile."""

from __future__ import annotations

import asyncio

import pytest
from biz_cap_comms import config
from biz_cap_comms.gate import GateConfig
from biz_cap_comms.tools import register
from biz_kernel.approvals import staged
from biz_kernel.contracts.ports import SendResult
from biz_kernel.health.models import Alert
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
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


# ------------------------------------------------- approver edits keep the operation's key


def _keyed_send_registry(tel: FakeTelephony) -> ToolRegistry:
    """A gated send that dedupes on the call's idempotency_key, as the port ledgers do."""

    def send(session, to: str, body: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        return asyncio.run(tel.send_sms(to, body, idempotency_key)).status

    reg = ToolRegistry()
    schema = {
        "type": "object",
        "properties": {"to": {}, "body": {}, "idempotency_key": {"type": "string"}},
        "required": ["to", "body", "idempotency_key"],
    }
    reg.register(Tool("demo.sms", "demo", SideEffect.EXTERNAL, schema, send))
    return reg


@pytest.mark.parametrize(
    "edited",
    [
        {"to": "+64210", "body": "Edited"},  # an editor that drops the key
        {"to": "+64210", "body": "Edited", "idempotency_key": "other"},  # or replaces it
    ],
    ids=["dropped", "replaced"],
)
def test_edit_keeps_idempotency_key(session, edited) -> None:  # type: ignore[no-untyped-def]
    tel = FakeTelephony()
    reg = _keyed_send_registry(tel)
    pol = PolicyEngine({"autonomy": {"default": 2}}, reg)
    args = {"to": "+64210", "body": "Original", "idempotency_key": "run1:reply"}
    op = staged.run_or_stage(session, reg, pol, "role_a", "demo.sms", args)
    assert isinstance(op, staged.StagedOperation)
    staged.approve(session, op.id, "owner", 1)
    # v1 reaches the provider, then the DB write is lost (a crash before commit)
    nested = session.begin_nested()
    staged.execute(session, op.id, reg, pol)
    nested.rollback()
    session.expire_all()
    assert len(tel.sent) == 1
    # the approver edits the still-approved operation: the version bumps, approval clears
    op = staged.edit(session, op.id, edited, by="owner", version=op.version)
    assert op.version == 2 and op.status == "pending" and op.approved_version is None
    assert op.inputs["idempotency_key"] == "run1:reply"
    assert op.inputs["body"] == "Edited"
    with pytest.raises(staged.NotApproved):
        staged.execute(session, op.id, reg, pol)
    staged.approve(session, op.id, "owner", 2)
    staged.execute(session, op.id, reg, pol)
    assert len(tel.sent) == 1  # the provider ledger deduped: an edit never double-sends


def test_edit_of_unkeyed_operation_adds_no_key(session) -> None:  # type: ignore[no-untyped-def]
    action = Action(actor="r", tool="t", side_effect=SideEffect.EXTERNAL, inputs={"a": 1})
    op = staged.stage(session, action, "owner")
    assert staged.edit(session, op.id, {"a": 2}, version=op.version).inputs == {"a": 2}
