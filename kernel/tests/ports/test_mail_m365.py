from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from biz_kernel.contracts.ports import OutboundMessage
from biz_kernel.contracts.suite import check_calendar_port, check_mail_port
from biz_port_mail_m365 import IDEMPOTENCY_HEADER, M365Mail, create
from biz_port_mail_m365.testing import FakeGraph

MSG = OutboundMessage(to=["a@example.com"], subject="Hi", body="Body")


def make(graph: FakeGraph, clock=None) -> M365Mail:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    kw = {"clock": clock} if clock else {}
    return M365Mail("tenant", "cid", "sec", "box@example.com", http=http, **kw)


async def test_passes_contract_suites() -> None:
    graph = FakeGraph()
    port = make(graph)
    await check_mail_port(port)
    await check_calendar_port(port)
    assert len(graph.sent_mail) == 2


async def test_send_sets_idempotency_header_once() -> None:
    graph = FakeGraph()
    port = make(graph)
    await port.send(MSG, "k1")
    await port.send(MSG, "k1")
    assert len(graph.sent_mail) == 1
    assert {"name": IDEMPOTENCY_HEADER, "value": "k1"} in graph.sent_mail[0][
        "internetMessageHeaders"
    ]


async def test_token_cached_until_expiry() -> None:
    graph = FakeGraph(expires_in=3600)
    now = [0.0]
    port = make(graph, clock=lambda: now[0])
    await port.fetch_new(datetime(2000, 1, 1, tzinfo=UTC))
    await port.fetch_new(datetime(2000, 1, 1, tzinfo=UTC))
    assert graph.token_calls == 1
    now[0] = 3600  # past expiry minus skew
    await port.fetch_new(datetime(2000, 1, 1, tzinfo=UTC))
    assert graph.token_calls == 2


async def test_timeout_after_send_is_uncertain_then_reconciled_not_resent() -> None:
    graph = FakeGraph()
    graph.fail_send = httpx.ReadTimeout("lost the answer")  # message went out
    port = make(graph)
    first = await port.send(MSG, "k1")
    assert first.status == "uncertain"
    graph.fail_send = None
    again = await port.send(MSG, "k1")  # reconciles against Sent Items
    assert again.status == "sent" and again.provider_id
    assert len(graph.sent_mail) == 1, "must not resend"


async def test_uncertain_stays_uncertain_when_not_in_sent_items() -> None:
    graph = FakeGraph()
    graph.drop_send = httpx.ReadTimeout("never arrived")
    port = make(graph)
    assert (await port.send(MSG, "k1")).status == "uncertain"
    graph.drop_send = None
    assert (await port.send(MSG, "k1")).status == "uncertain"
    assert graph.sent_mail == []  # still never auto-retried


async def test_connect_timeout_is_failed() -> None:
    graph = FakeGraph()
    graph.drop_send = httpx.ConnectTimeout("no route")
    assert (await make(graph).send(MSG, "k")).status == "failed"


async def test_reconcile() -> None:
    graph = FakeGraph()
    port = make(graph)
    assert await port.reconcile("nope") is None
    await port.send(MSG, "k1")
    found = await port.reconcile("k1")
    assert found is not None and found.status == "sent"
    assert await port.reconcile("k2") is None


async def test_fetch_new_filter_and_mapping() -> None:
    graph = FakeGraph()
    graph.inbox = [
        {
            "id": "m1",
            "subject": "Leak",
            "body": {"contentType": "html", "content": "<p>Help</p>"},
            "from": {"emailAddress": {"address": "c@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "box@example.com"}}],
            "receivedDateTime": "2030-01-01T10:00:00.0000000Z",
            "conversationId": "t1",
        }
    ]
    out = await make(graph).fetch_new(datetime(2029, 12, 31, tzinfo=UTC))
    assert out[0].body == "Help" and out[0].sender == "c@example.com" and out[0].thread_id == "t1"
    req = graph.requests[-1]
    assert "receivedDateTime gt 2029-12-31T00:00:00Z" in req.url.params["$filter"]
    assert "/users/box@example.com/mailFolders/inbox/messages" in req.url.path


async def test_free_slots_respect_working_hours() -> None:
    graph = FakeGraph()
    port = make(graph)
    start = datetime(2030, 1, 7, tzinfo=UTC)  # Monday
    slots = await port.free_slots(start, start.replace(day=13), 60)
    assert len(slots) == 8 * 5  # Mon-Fri 09-17, no weekend
    assert slots[0].start.hour == 9 and slots[-1].end.hour == 17


async def test_book_sends_transaction_id() -> None:
    graph = FakeGraph()
    port = make(graph)
    from biz_kernel.contracts.ports import CalendarEvent

    s = datetime(2030, 1, 7, 9, tzinfo=UTC)
    ev = CalendarEvent(title="T", start=s, end=s.replace(hour=10))
    a, b = await port.book(ev, "tx"), await port.book(ev, "tx")
    assert a == b and len(graph.events) == 1 and graph.events["tx"]["transactionId"] == "tx"


def test_create_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "BIZ_M365_TENANT": "t",
        "BIZ_M365_CLIENT_ID": "c",
        "BIZ_M365_SECRET": "s",
        "BIZ_M365_MAILBOX": "m@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    assert create().mailbox == "m@example.com"
