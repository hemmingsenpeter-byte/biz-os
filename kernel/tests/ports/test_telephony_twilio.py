from __future__ import annotations

import pytest
import requests
from biz_kernel.contracts.suite import check_telephony_port
from biz_port_telephony_twilio import TwilioTelephony, WebhookRejected, create
from biz_port_telephony_twilio.testing import FakeTwilioClient, signed_request
from twilio.base.exceptions import TwilioRestException

TOKEN = "secret-token"
URL = "https://hooks.example.com/twilio"


def make(client: FakeTwilioClient | None = None) -> tuple[TwilioTelephony, FakeTwilioClient]:
    c = client or FakeTwilioClient()
    return TwilioTelephony("AC1", TOKEN, "+15550000", client=c, webhook_url=URL), c


async def test_passes_contract_suite() -> None:
    port, client = make()
    sample = signed_request(TOKEN, URL, {"CallStatus": "no-answer", "From": "+1", "To": "+2"})
    await check_telephony_port(port, sample)
    assert len(client.sent) == 1


async def test_same_key_sends_once() -> None:
    port, client = make()
    a = await port.send_sms("+1", "hi", "k")
    b = await port.send_sms("+1", "hi", "k")
    assert a == b and a.status == "sent" and len(client.sent) == 1


async def test_timeout_is_uncertain_and_never_retried() -> None:
    client = FakeTwilioClient(error=requests.exceptions.ReadTimeout("slow"))
    port, _ = make(client)
    first = await port.send_sms("+1", "hi", "k")
    assert first.status == "uncertain"
    client.error = None
    again = await port.send_sms("+1", "hi", "k")
    assert again == first and client.sent == []


async def test_connect_timeout_and_api_error_are_failed() -> None:
    port, _ = make(FakeTwilioClient(error=requests.exceptions.ConnectTimeout("no")))
    assert (await port.send_sms("+1", "x", "k1")).status == "failed"
    err = TwilioRestException(400, "u", "bad number")
    port, _ = make(FakeTwilioClient(error=err))
    assert (await port.send_sms("+1", "x", "k2")).status == "failed"


def test_invalid_signature_rejected() -> None:
    port, _ = make()
    form = {"Body": "hi", "MessageSid": "SM1", "From": "+1", "To": "+2"}
    with pytest.raises(WebhookRejected):
        port.parse_webhook(signed_request(TOKEN, URL, form, signature="bogus"))
    with pytest.raises(WebhookRejected):
        port.parse_webhook({"form": form, "headers": {}, "url": URL})
    with pytest.raises(WebhookRejected):  # tampered body
        req = signed_request(TOKEN, URL, form)
        req["form"] = {**form, "Body": "evil"}
        port.parse_webhook(req)
    with pytest.raises(WebhookRejected):  # signed with the wrong token
        port.parse_webhook(signed_request("other", URL, form))
    with pytest.raises(WebhookRejected):
        port.parse_webhook("nonsense")


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_missed_call(status: str) -> None:
    port, _ = make()
    form = {"CallStatus": status, "CallSid": "CA1", "From": "+1", "To": "+2"}
    ev = port.parse_webhook(signed_request(TOKEN, URL, form))
    assert (ev.kind, ev.from_number, ev.to_number, ev.provider_id) == (
        "missed_call",
        "+1",
        "+2",
        "CA1",
    )


def test_inbound_sms_and_other() -> None:
    port, _ = make()
    sms = {"Body": "Hello", "MessageSid": "SM9", "From": "+1", "To": "+2"}
    ev = port.parse_webhook(signed_request(TOKEN, URL, sms))
    assert ev.kind == "inbound_sms" and ev.body == "Hello" and ev.provider_id == "SM9"
    done = {"CallStatus": "completed", "CallSid": "CA2", "From": "+1", "To": "+2"}
    assert port.parse_webhook(signed_request(TOKEN, URL, done)).kind == "other"


def test_create_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "BIZ_TWILIO_SID": "AC1",
        "BIZ_TWILIO_TOKEN": TOKEN,
        "BIZ_TWILIO_FROM": "+15550000",
        "BIZ_PUBLIC_URL": URL,
    }.items():
        monkeypatch.setenv(k, v)
    port = create()
    assert port.webhook_url == URL and port.from_number == "+15550000"
