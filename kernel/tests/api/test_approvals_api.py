import uuid
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
from biz_kernel.workflows.models import SopRun, SopStep
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


def test_approval_cards_link_to_approval_task_and_run(client, session):
    owner = create_user(session, "o5@example.com", "owner", "O")
    task = Task(title="reply", status="open")
    session.add(task)
    session.flush()
    job_id = uuid.uuid4()
    waiting, approval = _op(session), _op(session)
    plain = _op(session)  # staged outside any run: it links only to itself
    session.add_all(
        [
            SopRun(
                id="r1",
                role="sales",
                sop="s",
                task_id=task.id,
                job_id=job_id,
                waiting_op_id=str(waiting.id),
            ),
            SopRun(id="r2", role="sales", sop="s", task_id=task.id),
        ]
    )
    session.flush()
    session.add(SopStep(run_id="r2", step_id="ok:request", output=str(approval.id)))
    session.flush()
    _login(client, owner)
    cards = {c["id"]: c for c in client.get("/v1/approvals").json()}
    w = cards[str(waiting.id)]
    assert w["run_id"] == "r1" and w["task_id"] == str(task.id) and w["job_id"] == str(job_id)
    assert w["links"] == {
        "self": f"/v1/approvals/{waiting.id}",
        "task": f"/v1/tasks/{task.id}",
        "job": f"/v1/jobs/{job_id}/timeline",
    }
    a = cards[str(approval.id)]
    assert a["run_id"] == "r2" and a["links"]["task"] == f"/v1/tasks/{task.id}"
    assert "job" not in a["links"]
    p = cards[str(plain.id)]
    assert p["run_id"] is None and p["links"] == {"self": f"/v1/approvals/{plain.id}"}
    # every link opens
    one = client.get(w["links"]["self"])
    assert one.status_code == 200 and one.json()["links"] == w["links"]
    assert client.get(w["links"]["task"]).json()["title"] == "reply"
    assert client.get(f"/v1/approvals/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/v1/tasks/{uuid.uuid4()}").status_code == 404
    # a staged tool call is still found after the run stops waiting on it
    run = session.get(SopRun, "r1")
    run.waiting_op_id = None
    session.add(
        SopStep(
            run_id="r1",
            step_id="send:staged",
            output={"status": "staged", "op_id": str(waiting.id)},
        )
    )
    session.flush()
    # decisions return the linked card too
    r = client.post(f"/v1/approvals/{waiting.id}/edit", json={"inputs": {"a": 3}})
    assert r.status_code == 200 and r.json()["links"] == w["links"]
