"""A successful HTTP response acknowledges a committed transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest
from biz_kernel import db
from biz_kernel.auth.deps import DbSession
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.mark.parametrize("commit_fails", [False, True])
def test_commit_finishes_before_http_acknowledgement(monkeypatch, commit_fails: bool) -> None:
    events: list[str] = []

    @contextmanager
    def session_scope() -> Iterator[Session]:
        events.append("begin")
        try:
            yield cast(Session, object())
            events.append("commit")
            if commit_fails:
                raise RuntimeError("database commit failed")
        finally:
            events.append("closed")

    monkeypatch.setattr(db, "session_scope", session_scope)
    app = FastAPI()

    @app.post("/change")
    def change(session: DbSession) -> dict[str, bool]:
        events.append("handler")
        return {"changed": True}

    async def observe(scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.start":
                events.append(f"send:{message['status']}")
            await send(message)

        await app(scope, receive, record)

    with TestClient(observe, raise_server_exceptions=False) as client:
        response = client.post("/change")
    status = 500 if commit_fails else 200
    assert response.status_code == status
    assert events.index("commit") < events.index(f"send:{status}")
    assert events.index("closed") < events.index(f"send:{status}")
