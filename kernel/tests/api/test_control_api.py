"""Kill switch and corrections over the API."""

from __future__ import annotations

import pytest
from biz_kernel import control
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import Correction
from biz_kernel.auth import tokens
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from biz_kernel.policy.types import Action, SideEffect
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def client(session):
    def decide(op_id, by, *, approve, version=None, reason=""):
        if approve:
            return staged.approve(session, op_id, by, version)
        return staged.reject(session, op_id, by, reason)

    app = create_app(decide=decide)
    extra = APIRouter()

    @extra.post("/acts")
    def acts() -> None:
        raise control.Paused("demo.send")

    app.include_router(extra)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c


def _login(client, user):
    client.cookies.set("biz_session", tokens.issue_session(user.id))


def _op(session, inputs=None):
    action = Action(
        actor="sales",
        tool="mail.send",
        side_effect=SideEffect.EXTERNAL,
        inputs=inputs or {"body": "draft", "idempotency_key": "k1"},
        decision_type="reply_sending",
    )
    return staged.stage(session, action, "owner", "needs ok")


def test_anyone_signed_in_pauses_only_the_owner_resumes(client, session):
    owner = create_user(session, "o@example.com", "owner", "O")
    staff = create_user(session, "s@example.com", "staff", "S")
    assert client.get("/v1/control").status_code == 401
    assert client.post("/v1/control/pause", json={}).status_code == 401
    _login(client, staff)
    assert client.get("/v1/control").json() == {
        "paused": False,
        "by": None,
        "reason": "",
        "at": None,
    }
    r = client.post("/v1/control/pause", json={"reason": "odd replies"})
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] and body["by"] == "s@example.com" and body["reason"] == "odd replies"
    assert body["at"] is not None
    assert client.post("/v1/control/resume").status_code == 403
    assert control.is_paused(session)
    _login(client, owner)
    assert client.post("/v1/control/resume").json()["paused"] is False
    assert not control.is_paused(session)


def test_nothing_is_approved_while_paused(client, session):
    owner = create_user(session, "o2@example.com", "owner", "O")
    op = _op(session)
    _login(client, owner)
    client.post("/v1/control/pause", json={})
    r = client.post(f"/v1/approvals/{op.id}/approve", json={"version": 1})
    assert r.status_code == 409 and "paused" in r.json()["detail"]
    edited = client.post(
        f"/v1/approvals/{op.id}/edit", json={"version": op.version, "inputs": {"body": "x"}}
    )
    assert edited.status_code == 200  # editing and rejecting stay possible
    client.post("/v1/control/resume")
    assert client.post(f"/v1/approvals/{op.id}/approve", json={"version": 2}).status_code == 200


def test_a_route_that_would_act_while_paused_answers_409(client):
    r = client.post("/acts")
    assert r.status_code == 409 and r.json() == {"detail": "paused: demo.send"}


def test_an_edit_is_kept_as_a_correction(client, session):
    owner = create_user(session, "o3@example.com", "owner", "O")
    op = _op(session)
    _login(client, owner)
    r = client.post(
        f"/v1/approvals/{op.id}/edit",
        json={
            "version": op.version,
            "inputs": {"body": "fixed", "idempotency_key": "changed"},
            "reason": "wrong opening time",
            "severity": "factual",
        },
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    (row,) = client.get("/v1/corrections").json()
    assert row["op_id"] == str(op.id) and row["actor"] == "sales" and row["by"] == "owner"
    assert row["tool"] == "mail.send" and row["decision_type"] == "reply_sending"
    assert (row["from_version"], row["to_version"]) == (1, 2)
    assert row["before"] == {"body": "draft", "idempotency_key": "k1"}
    assert row["after"] == {"body": "fixed", "idempotency_key": "k1"}  # the key is kept
    assert row["reason"] == "wrong opening time" and row["severity"] == "factual"

    # A second edit is a second correction; severity is optional (untagged counts as an error).
    client.post(
        f"/v1/approvals/{op.id}/edit",
        json={"version": op.version, "inputs": {"body": "fixed again"}},
    )
    rows = client.get("/v1/corrections").json()
    assert [c["severity"] for c in rows] == [None, "factual"]  # newest first
    bad = client.post(
        f"/v1/approvals/{op.id}/edit",
        json={"version": op.version, "inputs": {"body": "y"}, "severity": "huge"},
    )
    assert bad.status_code == 422
    assert client.get("/v1/corrections?limit=0").status_code == 422


def test_what_is_not_a_correction(session):
    op = _op(session)
    staged.edit(
        session, op.id, {"body": "role rewrote its own draft"}, by="sales", version=op.version
    )
    staged.edit(session, op.id, {"body": "unattributed"}, version=op.version)
    same = dict(session.get(type(op), op.id).inputs)
    staged.edit(
        session, op.id, same, by="owner", severity="cosmetic", version=op.version
    )  # nothing changed
    assert session.scalars(select(Correction)).all() == []
    with pytest.raises(staged.ApprovalError):
        staged.edit(session, op.id, {"body": "z"}, by="owner", severity="huge", version=op.version)


def test_corrections_are_append_only(session):
    op = _op(session)
    staged.edit(
        session, op.id, {"body": "fixed"}, by="owner", severity="policy", version=op.version
    )
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(Correction.__table__.update().values(severity="cosmetic"))
