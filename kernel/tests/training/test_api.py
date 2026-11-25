"""Training source context and decisions belong to the verified owner."""

from __future__ import annotations

import uuid

import pytest
from biz_kernel import control
from biz_kernel.api import routes_training
from biz_kernel.auth import config, tokens
from biz_kernel.auth.models import User
from biz_kernel.training import service
from biz_kernel.training.models import TrainingCandidate, TrainingReview
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .conftest import EXPECTED, source


@pytest.fixture
def api(client: TestClient) -> TestClient:
    assert isinstance(client.app, FastAPI)
    if not any(
        getattr(route, "path", "") == "/v1/training/corrections" for route in client.app.routes
    ):
        client.app.include_router(routes_training.router(), prefix="/v1")
    return client


def signin(client: TestClient, session: Session, role: str = "owner") -> User:
    user = User(email=f"{uuid.uuid4()}@example.invalid", role=role, display_name="Test operator")
    session.add(user)
    session.flush()
    client.cookies.set(config.SESSION_COOKIE, tokens.issue_session(user.id))
    return user


@pytest.mark.parametrize("role,expected_status", [(None, 401), ("staff", 403)])
def test_all_training_reads_and_mutations_require_the_owner(
    api: TestClient, session: Session, role: str | None, expected_status: int
) -> None:
    if role:
        signin(api, session, role)
    identifier = uuid.uuid4()
    for path in (
        "/training/corrections",
        f"/training/corrections/{identifier}",
        "/training/candidates",
        f"/training/candidates/{identifier}",
    ):
        assert api.get("/v1" + path).status_code == expected_status
    for path, body in (
        (f"/training/corrections/{identifier}/candidates", {"rule": "A rule."}),
        (f"/training/candidates/{identifier}/review", {}),
        (f"/training/candidates/{identifier}/reject", {}),
    ):
        assert api.post("/v1" + path, json=body).status_code == expected_status


def test_owner_can_inspect_source_and_propose_reviewed_case(
    api: TestClient, session: Session
) -> None:
    owner = signin(api, session)
    world = source(session)
    correction_id = str(world["correction"].id)
    snapshot_id = str(world["snapshot"].id)
    context = api.get(f"/v1/training/corrections/{correction_id}").json()
    assert context["correction"]["before"] != context["correction"]["after"]
    assert context["candidate_id"] is None
    assert [item["id"] for item in context["snapshots"]] == [snapshot_id]
    assert context["snapshots"][0]["model_revision"] is None
    assert len(api.get("/v1/training/corrections").json()) == 1
    body = {
        "rule": "Ask for evidence before making a commitment.",
        "snapshot_id": snapshot_id,
        "expected_output": EXPECTED,
    }
    response = api.post(f"/v1/training/corrections/{correction_id}/candidates", json=body)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["created_by"] == str(owner.id)
    assert candidate["status"] == "proposed" and candidate["eligible_for_testing"] is True
    assert candidate["readiness_established"] is False
    assert candidate["regression_case"]["private"] is True
    same = api.post(f"/v1/training/corrections/{correction_id}/candidates", json=body)
    assert same.json()["id"] == candidate["id"]
    assert (
        api.post(
            f"/v1/training/corrections/{correction_id}/candidates",
            json={**body, "rule": "Different."},
        ).status_code
        == 409
    )
    assert len(api.get("/v1/training/candidates").json()) == 1
    identifier = candidate["id"]
    control.pause(session, "owner")
    assert api.post(f"/v1/training/candidates/{identifier}/review", json={}).status_code == 409
    assert service.review_for(session, uuid.UUID(identifier)) is None
    control.resume(session, "owner")
    reviewed = api.post(
        f"/v1/training/candidates/{identifier}/review", json={"reason": "Test this case."}
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "accepted_for_testing"
    assert reviewed.json()["review"]["by"] == str(owner.id)
    assert reviewed.json()["regression_case"]["owner_reviewed"] is False
    assert api.get(f"/v1/training/candidates/{identifier}").json()["readiness_established"] is False
    assert api.post(f"/v1/training/candidates/{identifier}/reject", json={}).status_code == 409
    assert session.scalar(select(func.count()).select_from(TrainingReview)) == 1


def test_legacy_candidate_is_incomplete_and_rejectable_while_paused(
    api: TestClient, session: Session
) -> None:
    signin(api, session)
    world = source(session, snapshot=False)
    correction_id = str(world["correction"].id)
    created = api.post(
        f"/v1/training/corrections/{correction_id}/candidates", json={"rule": "Review this rule."}
    )
    assert created.status_code == 200
    candidate = created.json()
    assert candidate["eligible_for_testing"] is False and len(candidate["incomplete_reasons"]) == 2
    assert candidate["snapshot"] is None and candidate["regression_case"] is None
    assert api.post(f"/v1/training/candidates/{candidate['id']}/review", json={}).status_code == 409
    control.pause(session, "owner")
    rejected = api.post(
        f"/v1/training/candidates/{candidate['id']}/reject", json={"reason": "No source."}
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


def test_bad_snapshot_selection_or_expected_output_is_a_clean_422(
    api: TestClient, session: Session
) -> None:
    signin(api, session)
    world, other = source(session), source(session)
    correction_id = str(world["correction"].id)
    base = {
        "rule": "A rule.",
        "snapshot_id": str(world["snapshot"].id),
        "expected_output": EXPECTED,
    }
    for body in (
        {**base, "snapshot_id": str(other["snapshot"].id)},
        {**base, "expected_output": {"body": 123}},
        {**base, "rule": "  "},
    ):
        response = api.post(f"/v1/training/corrections/{correction_id}/candidates", json=body)
        assert response.status_code == 422, response.text
    assert session.scalar(select(func.count()).select_from(TrainingCandidate)) == 0
    assert api.get(f"/v1/training/candidates/{uuid.uuid4()}").status_code == 404


def test_expected_output_activating_missing_local_reference_is_sanitized(
    api: TestClient, session: Session
) -> None:
    signin(api, session)
    schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"enum": ["base", "changed"]}},
        "if": {"properties": {"kind": {"const": "changed"}}},
        "then": {
            "properties": {"detail": {"$ref": "#/$defs/private-missing"}},
            "required": ["detail"],
        },
    }
    world = source(session, schema=schema, output={"kind": "base"})
    response = api.post(
        f"/v1/training/corrections/{world['correction'].id}/candidates",
        json={
            "rule": "A rule.",
            "snapshot_id": str(world["snapshot"].id),
            "expected_output": {"kind": "changed", "detail": "unresolved private value"},
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "model output does not match the captured output schema"
    assert (
        "unresolved private value" not in response.text and "private-missing" not in response.text
    )
    assert session.scalar(select(func.count()).select_from(TrainingCandidate)) == 0
