from datetime import UTC, datetime
from decimal import Decimal

import pytest
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged, suggestions
from biz_kernel.approvals.models import StagedOperation
from biz_kernel.auth import tokens
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from biz_kernel.goals.models import Goal, GoalReading
from biz_kernel.health.service import raise_alert
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.records.models import Task
from fastapi.testclient import TestClient


@pytest.fixture
def decisions(session):
    return []


@pytest.fixture
def client(session, decisions):
    def decide(op_id, by, *, approve, version=None, reason=""):
        decisions.append((op_id, by, approve))
        if approve:
            return staged.approve(session, op_id, by, version)
        return staged.reject(session, op_id, by, reason)

    app = create_app(decide=decide)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c


def _login(client, user):
    client.cookies.set("biz_session", tokens.issue_session(user.id))


def _op(session, approver="owner"):
    action = Action(
        actor="sales", tool="mail.send", side_effect=SideEffect.EXTERNAL, inputs={"a": 1}
    )
    return staged.stage(session, action, approver, "needs ok")


def test_queue_and_approve(client, session, decisions):
    owner = create_user(session, "o@example.com", "owner", "O")
    op = _op(session)
    _login(client, owner)
    rows = client.get("/v1/approvals").json()
    assert rows[0]["tool"] == "mail.send" and rows[0]["version"] == 1 and rows[0]["reason"]
    r = client.post(f"/v1/approvals/{op.id}/approve", json={"version": 1})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert decisions[0][1:] == ("owner", True)


def test_stale_version_is_409(client, session):
    owner = create_user(session, "o2@example.com", "owner", "O")
    op = _op(session)
    _login(client, owner)
    r = client.post(f"/v1/approvals/{op.id}/edit", json={"inputs": {"a": 2}})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.post(f"/v1/approvals/{op.id}/approve", json={"version": 1}).status_code == 409
    assert client.post(f"/v1/approvals/{op.id}/approve", json={"version": 2}).status_code == 200


def test_staff_can_view_but_not_decide(client, session):
    staff = create_user(session, "s@example.com", "staff", "S")
    op = _op(session)
    _login(client, staff)
    assert len(client.get("/v1/approvals").json()) == 1
    assert client.post(f"/v1/approvals/{op.id}/approve", json={"version": 1}).status_code == 403
    assert client.post(f"/v1/approvals/{op.id}/reject", json={}).status_code == 403
    assert client.post(f"/v1/approvals/{op.id}/edit", json={"inputs": {}}).status_code == 403
    assert session.get(StagedOperation, op.id).status == "pending"


def test_named_approver_may_decide_and_reject(client, session):
    staff = create_user(session, "boss@example.com", "staff", "B")
    op = _op(session, approver="boss@example.com")
    _login(client, staff)
    r = client.post(f"/v1/approvals/{op.id}/reject", json={"reason": "no"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_unauthenticated(client):
    assert client.get("/v1/approvals").status_code == 401


def test_suggestions(client, session):
    owner = create_user(session, "o3@example.com", "owner", "O")
    staff = create_user(session, "s3@example.com", "staff", "S")
    s1 = suggestions.propose(session, "planner", "T1", "mail.send", {"x": 1}, ["why"])
    s2 = suggestions.propose(session, "planner", "T2", "mail.send", {"x": 2}, ["why"])
    _login(client, staff)
    assert len(client.get("/v1/suggestions").json()) == 2
    assert client.post(f"/v1/suggestions/{s1.id}/approve").status_code == 403
    _login(client, owner)
    r = client.post(f"/v1/suggestions/{s1.id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.post(f"/v1/suggestions/{s1.id}/approve").status_code == 409
    assert client.post(f"/v1/suggestions/{s2.id}/reject").json()["status"] == "rejected"


def test_scorecard_alerts_tasks(client, session):
    staff = create_user(session, "s4@example.com", "staff", "S")
    g = Goal(key="rev", target=Decimal("10"))
    session.add(g)
    session.flush()
    session.add(GoalReading(goal_id=g.id, value=Decimal("10"), at=datetime(2020, 1, 1, tzinfo=UTC)))
    raise_alert(session, "schedule.missed", "x", "late", datetime.now(UTC))
    session.add_all([Task(title="a", status="open"), Task(title="b", status="cancelled")])
    session.flush()
    _login(client, staff)
    card = client.get("/v1/goals/scorecard").json()
    assert card[0]["key"] == "rev" and card[0]["rag"] == "green"
    assert client.get("/v1/health/alerts").json()[0]["kind"] == "schedule.missed"
    assert [t["title"] for t in client.get("/v1/tasks", params={"status": "open"}).json()] == ["a"]
