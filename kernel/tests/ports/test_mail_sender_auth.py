"""Sender verification: the receiving server's Authentication-Results verdict on inbound mail."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from biz_kernel.contracts.ports import InboundMessage, SenderAuth
from biz_kernel.contracts.suite import check_mail_port, check_sender_auth
from biz_port_fakes import FakeMail
from biz_port_mail_m365 import M365Mail, create
from biz_port_mail_m365.auth_results import parse, trusted_auth
from biz_port_mail_m365.testing import FakeGraph

OURS = "mx.box.example"
PASS = (
    f"{OURS}; spf=pass (sender IP is 192.0.2.1; ok) smtp.mailfrom=client.example;"
    " dkim=pass (signature was verified) header.d=client.example;"
    "dmarc=pass action=none header.from=client.example"
)


def _h(value: str, name: str = "Authentication-Results") -> dict[str, str]:
    return {"name": name, "value": value}


def test_parses_results_and_ignores_comments() -> None:
    auth = trusted_auth([_h(PASS)], OURS)
    assert auth == SenderAuth(
        dmarc="pass", spf="pass", dkim="pass", from_domain="client.example", authserv_id=OURS
    )
    assert auth.verifies("Someone@Client.Example")
    assert not auth.verifies("someone@other.example")  # aligned with the From domain only


@pytest.mark.parametrize(
    ("value", "dmarc", "spf", "dkim"),
    [
        (f"{OURS}; dmarc=fail header.from=client.example", "fail", "none", "none"),
        (
            f"{OURS} 1; spf=softfail smtp.mailfrom=x; dmarc=none header.from=x",
            "none",
            "fail",
            "none",
        ),
        (f"{OURS}; none", "none", "none", "none"),
        (f"{OURS}; dkim=fail header.d=a; dkim=pass header.d=b; dmarc=pass", "fail", "none", "pass"),
        (f'{OURS}; DMARC=Pass header.from="client.example"', "pass", "none", "none"),
        (f"{OURS}; dmarc=temperror header.from=client.example", "fail", "none", "none"),
    ],
)
def test_verdicts(value: str, dmarc: str, spf: str, dkim: str) -> None:
    auth = trusted_auth([_h(value)], OURS)
    assert (auth.dmarc, auth.spf, auth.dkim) == (dmarc, spf, dkim)


def test_dmarc_pass_without_one_from_domain_is_not_a_pass() -> None:
    two = f"{OURS}; dmarc=pass header.from=a.example; dmarc=pass header.from=b.example"
    assert trusted_auth([_h(two)], OURS).dmarc == "fail"
    assert trusted_auth([_h(f"{OURS}; dmarc=pass")], OURS).dmarc == "fail"


def test_only_our_servers_header_is_trusted() -> None:
    forged = PASS.replace(OURS, "mx.attacker.example")
    assert trusted_auth([_h(forged)], OURS) == SenderAuth()
    # Without a configured authserv-id nothing is trusted.
    assert trusted_auth([_h(PASS)], "") == SenderAuth()
    # A header with no authserv-id at all could have been written by anyone.
    no_id = PASS.split(";", 1)[1]
    assert parse(no_id) is not None and parse(no_id).authserv_id == ""  # type: ignore[union-attr]
    assert trusted_auth([_h(no_id)], OURS) == SenderAuth()
    # Other header names (ARC, renamed originals) are not the receiving server's verdict.
    for name in ("ARC-Authentication-Results", "Authentication-Results-Original"):
        assert trusted_auth([_h(PASS, name)], OURS) == SenderAuth()


def test_topmost_header_wins_over_one_the_sender_inserted() -> None:
    ours_fail = f"{OURS}; dmarc=fail header.from=client.example"
    # The receiving server prepends its header; the sender's forgery sits below it.
    assert trusted_auth([_h(ours_fail), _h(PASS)], OURS).dmarc == "fail"
    assert trusted_auth([_h(PASS), _h(ours_fail)], OURS).dmarc == "pass"


@pytest.mark.parametrize("value", ["", "   ", "x" * 9000, f"{OURS} 1 extra; dmarc=pass"])
def test_unusable_headers_are_unverified(value: str) -> None:
    assert trusted_auth([_h(value)], OURS) == SenderAuth()
    assert trusted_auth([None, "junk", {"name": None}], OURS) == SenderAuth()  # type: ignore[list-item]


def _graph_msg(mid: str, sender: str, headers: list[dict[str, str]] | None) -> dict:  # type: ignore[type-arg]
    m = {
        "id": mid,
        "subject": "s",
        "body": {"contentType": "text", "content": "b"},
        "from": {"emailAddress": {"address": sender}},
        "toRecipients": [{"emailAddress": {"address": "box@example.com"}}],
        "receivedDateTime": "2030-01-01T10:00:00.0000000Z",
    }
    if headers is not None:
        m["internetMessageHeaders"] = headers
    return m


def _port(graph: FakeGraph, authserv_id: str = OURS) -> M365Mail:
    http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    return M365Mail("t", "c", "s", "box@example.com", http=http, authserv_id=authserv_id)


async def test_m365_fetch_new_carries_the_verdict() -> None:
    graph = FakeGraph()
    graph.inbox = [
        _graph_msg("ok", "a@client.example", [_h("Received: x", "Received"), _h(PASS)]),
        _graph_msg("spoof", "ceo@bank.example", [_h(PASS)]),  # pass is for another domain
        _graph_msg("none", "a@client.example", None),
    ]
    out = {m.provider_id: m for m in await _port(graph).fetch_new(datetime(2029, 1, 1, tzinfo=UTC))}
    assert out["ok"].auth.verifies(out["ok"].sender)
    assert out["spoof"].auth.dmarc == "pass" and not out["spoof"].auth.verifies("ceo@bank.example")
    assert out["none"].auth == SenderAuth()
    assert "internetMessageHeaders" in graph.requests[-1].url.params["$select"]
    await check_mail_port(_port(graph))  # the contract suite checks each fetched verdict


async def test_m365_without_configured_authserv_id_never_verifies() -> None:
    graph = FakeGraph()
    graph.inbox = [_graph_msg("ok", "a@client.example", [_h(PASS)])]
    (m,) = await _port(graph, authserv_id="").fetch_new(datetime(2029, 1, 1, tzinfo=UTC))
    assert m.auth == SenderAuth() and not m.auth.verifies(m.sender)


def test_create_reads_authserv_id(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "BIZ_M365_TENANT": "t",
        "BIZ_M365_CLIENT_ID": "c",
        "BIZ_M365_SECRET": "s",
        "BIZ_M365_MAILBOX": "m@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BIZ_M365_AUTHSERV_ID", raising=False)
    assert create().authserv_id == ""
    monkeypatch.setenv("BIZ_M365_AUTHSERV_ID", " mx.box.example ")
    assert create().authserv_id == "mx.box.example"


def _inbound(sender: str = "a@client.example") -> InboundMessage:
    return InboundMessage(
        provider_id="p1",
        sender=sender,
        to=["box@example.com"],
        subject="s",
        body="b",
        received_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


async def test_fake_mail_sets_the_verdict_and_passes_the_suite() -> None:
    mail = FakeMail()
    mail.inject_inbound(_inbound())
    verdict = SenderAuth(dmarc="pass", from_domain="client.example", authserv_id="fake")
    mail.inject_inbound(_inbound().model_copy(update={"provider_id": "p2"}), auth=verdict)
    plain, verified = await mail.fetch_new(datetime(2000, 1, 1, tzinfo=UTC))
    assert plain.auth == SenderAuth() and not plain.auth.verifies(plain.sender)
    assert verified.auth.verifies(verified.sender)
    await check_mail_port(mail)


def test_contract_check_rejects_a_bad_verdict() -> None:
    msg = _inbound().model_copy(update={"auth": SenderAuth.model_construct(dmarc="maybe")})
    with pytest.raises(AssertionError):
        check_sender_auth(msg)


@pytest.mark.parametrize(
    ("sender", "from_domain", "dmarc", "ok"),
    [
        ("a@client.example", "client.example", "pass", True),
        ("a@CLIENT.example.", "client.example", "pass", True),
        ("a@client.example", "client.example", "fail", False),
        ("a@client.example", "client.example", "none", False),
        ("a@sub.client.example", "client.example", "pass", False),  # exact From domain only
        ("a@client.example", "", "pass", False),
        ("client.example", "client.example", "pass", False),
        ("a@", "", "pass", False),
    ],
)
def test_verifies_needs_a_pass_for_the_same_from_domain(
    sender: str, from_domain: str, dmarc: str, ok: bool
) -> None:
    auth = SenderAuth(dmarc=dmarc, from_domain=from_domain)  # type: ignore[arg-type]
    assert auth.verifies(sender) is ok
