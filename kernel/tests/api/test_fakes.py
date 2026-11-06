"""Run the contract suite against every fake."""

import uuid
from datetime import UTC, datetime

import pytest
from biz_kernel.contracts import suite
from biz_kernel.contracts.ports import (
    AccountingPort,
    InboundMessage,
    MailPort,
    ModelPort,
    SignPort,
    SpeechPort,
    TelephonyPort,
)
from biz_port_fakes import FAKES, FakeMail, FakeModel


async def test_mail():
    port = FakeMail()
    port.inject_inbound(
        InboundMessage(
            provider_id="m1",
            sender="x@example.com",
            to=["y@example.com"],
            subject="s",
            body="b",
            received_at=datetime.now(UTC),
        )
    )
    await suite.check_mail_port(port)
    assert len(await port.fetch_new(suite.EPOCH)) == 1


async def test_mail_scripted_failure_then_ok():
    from biz_kernel.contracts.ports import OutboundMessage

    port = FakeMail()
    port.fail_next = "failed"
    msg = OutboundMessage(to=["a@example.com"], subject="s", body="b")
    assert (await port.send(msg, "k")).status == "failed"
    assert (await port.send(msg, "k")).status == "sent"


async def test_telephony():
    port = FAKES["fake_telephony"]()
    await suite.check_telephony_port(port, {"kind": "missed_call", "from": "+1", "to": "+2"})


async def test_accounting():
    await suite.check_accounting_port(FAKES["fake_accounting"]())


async def test_sign():
    await suite.check_sign_port(FAKES["fake_sign"](), {"kind": "signed", "request_id": "r"})


async def test_model():
    await suite.check_model_port(FakeModel())


async def test_model_script_records_and_fails():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    m = FakeModel([{"a": "canned"}])
    assert await m.structured("s", [], schema, "local") == {"a": "canned"}
    assert m.calls[0]["tier"] == "local"
    m.fail_with = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await m.structured("s", [], schema, "cloud")
    m.fail_with = None
    m.script({"a": 1})  # invalid against schema -> rejected
    with pytest.raises(Exception):  # noqa: B017
        await m.structured("s", [], schema, "cloud")


async def test_speech():
    await suite.check_speech_port(FAKES["fake_speech"]())


def test_fakes_satisfy_protocols():
    protos = {
        "fake_mail": MailPort,
        "fake_telephony": TelephonyPort,
        "fake_accounting": AccountingPort,
        "fake_sign": SignPort,
        "fake_model": ModelPort,
        "fake_speech": SpeechPort,
    }
    assert set(protos) == set(FAKES)
    for name, proto in protos.items():
        assert isinstance(FAKES[name](), proto)
    assert uuid.uuid4()
