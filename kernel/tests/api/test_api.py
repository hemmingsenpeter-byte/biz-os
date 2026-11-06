import uuid

from biz_kernel.audit.log import record
from biz_kernel.auth import tokens
from biz_kernel.auth.users import create_user
from biz_kernel.records.models import Job


def _login(client, user):
    client.cookies.set("biz_session", tokens.issue_session(user.id))


def test_health_and_openapi(client):
    assert client.get("/v1/health").json() == {"status": "ok"}
    paths = client.get("/v1/openapi.json").json()["paths"]
    assert "/v1/packets" in paths and "/v1/audit" in paths


def test_packets_require_auth(client):
    assert client.get("/v1/packets", params={"job_id": str(uuid.uuid4())}).status_code == 401


def test_timeline_for_signed_in_staff(client, session):
    staff = create_user(session, "s@example.com", "staff", "S")
    job = Job(kind="k", stage="new")
    session.add(job)
    session.flush()
    _login(client, staff)
    r = client.get(f"/v1/jobs/{job.id}/timeline")
    assert r.status_code == 200 and r.json() == []
    r = client.get("/v1/packets", params={"job_id": str(job.id)})
    assert r.status_code == 200


def test_audit_owner_only(client, session):
    staff = create_user(session, "s2@example.com", "staff", "S")
    owner = create_user(session, "o@example.com", "owner", "O")
    record(session, "test", "demo", "subject")
    assert client.get("/v1/audit").status_code == 401
    _login(client, staff)
    assert client.get("/v1/audit").status_code == 403
    _login(client, owner)
    r = client.get("/v1/audit", params={"kind": "demo"})
    assert r.status_code == 200 and r.json()[0]["subject"] == "subject"


def test_mount_capability(client):
    from biz_kernel.api.app import create_app, mount_capability
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    router = APIRouter()
    router.get("/ping")(lambda: {"pong": True})
    app = create_app()
    mount_capability(app, "demo", router)
    assert TestClient(app).get("/v1/demo/ping").json() == {"pong": True}
