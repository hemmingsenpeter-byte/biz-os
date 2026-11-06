"""Every port Protocol has a contract-suite check and at least one fake; every fake passes it."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import biz_port_fakes
import pytest
from biz_kernel.contracts import ports, suite

# port kind -> (Protocol, suite check, sample webhook body or None). Explicit on purpose.
SUITE: dict[str, tuple[type, Callable[..., Any], Any]] = {
    "mail": (ports.MailPort, suite.check_mail_port, None),
    "calendar": (ports.CalendarPort, suite.check_calendar_port, None),
    "telephony": (
        ports.TelephonyPort,
        suite.check_telephony_port,
        {"kind": "missed_call", "from": "+1555", "to": "+1556"},
    ),
    "accounting": (ports.AccountingPort, suite.check_accounting_port, None),
    "sign": (ports.SignPort, suite.check_sign_port, {"kind": "signed", "request_id": "r1"}),
    "model": (ports.ModelPort, suite.check_model_port, None),
    "speech": (ports.SpeechPort, suite.check_speech_port, None),
    "storage": (ports.StoragePort, suite.check_storage_port, None),
}
FAKE_KIND = {f"fake_{k}": k for k in SUITE}


def _protocols() -> list[type]:
    return [
        c
        for _, c in inspect.getmembers(ports, inspect.isclass)
        if getattr(c, "_is_protocol", False)
        and c.__module__ == ports.__name__
        and c.__name__.endswith("Port")
    ]


def _run(kind: str, port: Any) -> None:
    _, check, sample = SUITE[kind]
    if "sample_webhook" in inspect.signature(check).parameters and sample is not None:
        asyncio.run(check(port, sample))
    else:
        result = check(port)
        if inspect.isawaitable(result):
            asyncio.run(result)


@pytest.mark.parametrize("proto", _protocols(), ids=lambda p: p.__name__)
def test_every_protocol_has_suite_and_fake(proto: type) -> None:
    kinds = [k for k, (p, _, _) in SUITE.items() if p is proto]
    assert kinds, f"{proto.__name__} has no suite function in SUITE"
    fakes = [n for n, c in biz_port_fakes.FAKES.items() if FAKE_KIND.get(n) == kinds[0]]
    assert fakes, f"{proto.__name__} has no fake in biz_port_fakes.FAKES"
    assert isinstance(biz_port_fakes.FAKES[fakes[0]](), proto)


def test_every_fake_is_mapped() -> None:
    assert set(biz_port_fakes.FAKES) == set(FAKE_KIND)


@pytest.mark.parametrize("name", sorted(biz_port_fakes.FAKES))
def test_fake_passes_contract_suite(name: str) -> None:
    _run(FAKE_KIND[name], biz_port_fakes.FAKES[name]())


def _twilio() -> Any:
    from biz_port_telephony_twilio import TwilioTelephony
    from biz_port_telephony_twilio.testing import FakeTwilioClient

    return TwilioTelephony(
        "AC1", "tok", "+15550000", client=FakeTwilioClient(), webhook_url="https://h.example/w"
    )


def _m365() -> Any:
    import httpx
    from biz_port_mail_m365 import M365Mail
    from biz_port_mail_m365.testing import FakeGraph

    http = httpx.AsyncClient(transport=httpx.MockTransport(FakeGraph()))
    return M365Mail("t", "c", "s", "box@example.com", http=http)


def test_twilio_adapter_passes_contract_suite() -> None:
    from biz_port_telephony_twilio.testing import signed_request

    sample = signed_request(
        "tok", "https://h.example/w", {"CallStatus": "busy", "From": "+1", "To": "+2"}
    )
    asyncio.run(suite.check_telephony_port(_twilio(), sample))


def test_m365_adapter_passes_mail_and_calendar_suites() -> None:
    asyncio.run(suite.check_mail_port(_m365()))
    asyncio.run(suite.check_calendar_port(_m365()))
