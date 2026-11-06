"""Test helper: an in-memory Graph + token endpoint usable as an httpx MockTransport handler."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx


class FakeGraph:
    def __init__(self, expires_in: int = 3600) -> None:
        self.token_calls = 0
        self.expires_in = expires_in
        self.sent_mail: list[dict[str, Any]] = []
        self.events: dict[str, dict[str, Any]] = {}  # transactionId -> event
        self.inbox: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.fail_send: Exception | None = None  # raised after the message is recorded
        self.drop_send: Exception | None = None  # raised before recording

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = unquote(req.url.path)
        if path.endswith("/oauth2/v2.0/token"):
            self.token_calls += 1
            return httpx.Response(
                200, json={"access_token": f"tok{self.token_calls}", "expires_in": self.expires_in}
            )
        assert req.headers["authorization"].startswith("Bearer tok")
        if path.endswith("/sendMail"):
            if self.drop_send:
                raise self.drop_send
            self.sent_mail.append(json.loads(req.content)["message"])
            if self.fail_send:
                raise self.fail_send
            return httpx.Response(202)
        if path.endswith("/mailFolders/sentitems/messages"):
            items = [
                {"id": f"AAMk{i}", "internetMessageHeaders": m["internetMessageHeaders"]}
                for i, m in enumerate(reversed(self.sent_mail))
            ]
            return httpx.Response(200, json={"value": items})
        if path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": self.inbox})
        if path.endswith("/calendar/getSchedule"):
            items = [
                {"status": "busy", "start": e["start"], "end": e["end"]}
                for e in self.events.values()
            ]
            return httpx.Response(200, json={"value": [{"scheduleItems": items}]})
        if path.endswith("/calendar/events"):
            body = json.loads(req.content)
            ev = self.events.setdefault(
                body["transactionId"], {**body, "id": f"EV{len(self.events) + 1}"}
            )
            return httpx.Response(201, json={"id": ev["id"], "webLink": "https://x/" + ev["id"]})
        return httpx.Response(404)
