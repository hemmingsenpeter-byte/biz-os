"""GET /metrics: signed-in users only, days 1 to 366, the agreed JSON shape."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from biz_kernel.api import routes_metrics
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth import tokens
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

TOP = {
    "since",
    "until",
    "decision_types",
    "serious_errors",
    "recorded_demotions",
    "tasks_completed",
    "cost_usd",
    "cost_per_task_usd",
    "time_saved_minutes",
}
ROW = {
    "role",
    "decision_type",
    "level",
    "proposals",
    "approved_unchanged",
    "approved_cosmetic",
    "approved_with_errors",
    "approved_unknown",
    "rejected",
    "pending",
    "approval_without_edit_rate",
    "error_free_rate",
}


def _client(session: Session, policy: dict[str, Any] | None) -> TestClient:
    app = FastAPI()
    app.state.manifest = None if policy is None else SimpleNamespace(policy=policy)
    app.include_router(routes_metrics.router(), prefix="/v1")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture
def api(session: Session) -> Iterator[TestClient]:
    with _client(session, None) as c:
        yield c


def _login(client: TestClient, session: Session, role: str = "staff") -> None:
    user = create_user(session, f"{role}@example.com", role, "U")
    client.cookies.set("biz_session", tokens.issue_session(user.id))


def test_requires_sign_in(api: TestClient) -> None:
    assert api.get("/v1/metrics").status_code == 401


def test_shape_and_default_window(api: TestClient, session: Session) -> None:
    session.add(
        StagedOperation(
            actor="role_a",
            tool="t.send",
            inputs={},
            inputs_sha256="0" * 64,
            approver="owner",
            status="approved",
            decision_type="reply",
        )
    )
    session.flush()
    _login(api, session)
    res = api.get("/v1/metrics")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == TOP
    (row,) = body["decision_types"]
    assert set(row) == ROW
    assert row["approved_unchanged"] == 1 and row["approval_without_edit_rate"] == 1.0
    assert row["level"] is None
    span = datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"])
    assert span == timedelta(days=28)
    assert body["cost_per_task_usd"] is None and body["time_saved_minutes"] is None


@pytest.mark.parametrize(("days", "status"), [(0, 422), (1, 200), (366, 200), (367, 422)])
def test_days_validation(api: TestClient, session: Session, days: int, status: int) -> None:
    _login(api, session)
    res = api.get("/v1/metrics", params={"days": days})
    assert res.status_code == status
    if status == 200:
        body = res.json()
        span = datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"])
        assert span == timedelta(days=days)


def test_days_must_be_a_number(api: TestClient, session: Session) -> None:
    _login(api, session)
    assert api.get("/v1/metrics", params={"days": "all"}).status_code == 422


def test_time_saved_uses_the_composition_baseline(session: Session) -> None:
    policy = {"metrics": {"baseline_minutes": {"default": 5}, "review_minutes_per_approval": 1}}
    with _client(session, policy) as c:
        _login(c, session, "owner")
        assert c.get("/v1/metrics").json()["time_saved_minutes"] == 0.0
    with _client(session, {}) as c:
        _login(c, session, "staff")
        assert c.get("/v1/metrics").json()["time_saved_minutes"] is None
