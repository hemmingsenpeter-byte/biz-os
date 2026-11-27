"""Version-specific authorization and private, durable comparison evidence."""

from __future__ import annotations

import uuid

import pytest
from biz_kernel import control
from biz_kernel.api import routes_comparisons
from biz_kernel.training import cohorts, service
from biz_kernel.training.models import (
    ComparisonJob,
    TrainingVersion,
)
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .conftest import EXPECTED, source
from .test_api import api as api
from .test_api import signin

MODEL = {
    "name": "fixture:local",
    "digest": "a" * 64,
    "ollama_version": "fixture-1",
    "backend_version": "0.1.0",
}


def root_candidate(session: Session, *, legacy: bool = False):  # type: ignore[no-untyped-def]
    world = source(session, snapshot=not legacy)
    root = service.create_candidate(
        session,
        world["correction"].id,
        rule="Ask for evidence.",
        by="owner",
        snapshot_id=world["snapshot"].id if world["snapshot"] else None,
        expected_output=EXPECTED if not legacy else None,
    )
    return world, root


def version(session: Session, root, *, parent=None, accepted=True, **changes):  # type: ignore[no-untyped-def]
    fields = {
        "parent_id": parent.id if parent else None,
        "rule": "Ask for evidence.",
        "system": "Draft a reply. Ask for evidence.",
        "expected_output": EXPECTED,
        "by": "owner",
        **changes,
    }
    row = service.create_version(session, root.id, **fields)
    if accepted:
        service.decide_version(session, row.id, decision="accepted_for_testing", by="owner")
    return row


def prepared(session: Session):  # type: ignore[no-untyped-def]
    world, root = root_candidate(session)
    row = version(session, root)
    cohort = cohorts.create_cohort(
        session, name="Private regression cases", version_ids=[row.id], by="owner"
    )
    cohorts.attest_cohort(session, cohort.id, by="owner")
    job = cohorts.enqueue(
        session,
        version_id=row.id,
        cohort_id=cohort.id,
        model=MODEL,
        idempotency_key="once",
        by="owner",
    )
    return world, root, row, cohort, job


def completed_results(session: Session, job):  # type: ignore[no-untyped-def]
    execution = cohorts.prepare_execution(session, job.id)
    cohorts.start_job(session, job.id)
    results = []
    for case in execution["cases"]:
        for side in ("baseline", "candidate"):
            claim = cohorts.begin_attempt(
                session, job.id, side=side, case_id=case["id"], repetition=1
            )
            result = {
                "side": side,
                "case_id": case["id"],
                "repetition": 1,
                "status": "completed",
                "passed": True,
                "score": 1.0,
                "output": case["expected"],
                "failures": [],
                "error": None,
                "latency_ms": 1,
                "api_cost_usd": "0",
                "hardware_cost_usd": None,
            }
            cohorts.finish_attempt(session, job.id, attempt_key=claim["attempt_key"], result=result)
            results.append(result)
    return results


def test_versions_preserve_roots_and_require_fresh_review(session: Session) -> None:
    _, root = root_candidate(session)
    service.decide(session, root.id, decision="accepted_for_testing", by="owner")
    first = version(session, root, accepted=False)
    assert service.version_review_for(session, first) is None
    with pytest.raises(service.TrainingConflict, match="own source-backed testing review"):
        cohorts.create_cohort(
            session, name="No inherited review", version_ids=[first.id], by="owner"
        )
    assert version(session, root, accepted=False).id == first.id
    with pytest.raises(service.TrainingConflict, match="stale"):
        version(session, root, accepted=False, system="Different concurrent edit.")
    service.decide_version(session, first.id, decision="rejected", by="owner")
    second = version(session, root, parent=first, expected_output={"body": "Revised outcome."})
    assert second.number == 2 and second.parent_id == first.id
    assert service.version_review_for(session, first).decision == "rejected"
    assert root.expected_output == EXPECTED
    assert session.scalar(select(func.count()).select_from(TrainingVersion)) == 2


def test_missing_provenance_and_confidence_only_checks_never_authorize(session: Session) -> None:
    _, root = root_candidate(session, legacy=True)
    row = version(session, root, accepted=False)
    assert service.version_incomplete_reasons(row, None)
    with pytest.raises(service.TrainingConflict, match="incomplete"):
        service.decide_version(session, row.id, decision="accepted_for_testing", by="owner")
    schema = {
        "type": "object",
        "required": ["body", "confidence"],
        "properties": {"body": {"type": "string"}, "confidence": {"type": "number"}},
    }
    world = source(session, schema=schema, output={"body": "Wrong", "confidence": 0.9})
    root2 = service.create_candidate(
        session,
        world["correction"].id,
        rule="A rule.",
        by="owner",
        snapshot_id=world["snapshot"].id,
        expected_output={"body": "Correct", "confidence": 0.9},
    )
    confidence = version(
        session,
        root2,
        accepted=False,
        expected_output={"body": "Correct", "confidence": 0.9},
        checks=[{"type": "json_schema"}, {"type": "numeric", "field": "confidence", "value": 0.9}],
    )
    assert service.version_incomplete_reasons(confidence, world["snapshot"])
    with pytest.raises(service.TrainingConflict, match="incomplete"):
        service.decide_version(session, confidence.id, decision="accepted_for_testing", by="owner")


def test_validating_descendant_checks_all_ancestor_contents(session: Session) -> None:
    _, root = root_candidate(session)
    first = version(session, root)
    second = version(session, root, parent=first)
    third = version(session, root, parent=second)
    with session.begin_nested():
        session.execute(
            text("ALTER TABLE training_versions DISABLE TRIGGER training_versions_append_only")
        )
        session.execute(
            text("UPDATE training_versions SET system='tampered' WHERE id=:id"), {"id": first.id}
        )
        session.execute(
            text("ALTER TABLE training_versions ENABLE TRIGGER training_versions_append_only")
        )
    session.expire_all()
    with pytest.raises(service.TrainingConflict, match="integrity"):
        service.validate_version(session, third)


def test_cohort_is_private_and_requires_attestation_of_exact_members(session: Session) -> None:
    _, root = root_candidate(session)
    row = version(session, root)
    cohort = cohorts.create_cohort(
        session, name="Regression cohort", version_ids=[row.id], by="owner"
    )
    assert (
        cohorts.create_cohort(
            session, name="Regression cohort", version_ids=[row.id], by="owner"
        ).id
        == cohort.id
    )
    with pytest.raises(service.TrainingConflict, match="own owner testing review"):
        cohorts.enqueue(
            session,
            version_id=row.id,
            cohort_id=cohort.id,
            model=MODEL,
            idempotency_key="unattested",
            by="owner",
        )
    cohorts.attest_cohort(session, cohort.id, by="owner")
    successor = version(session, root, parent=row)
    with pytest.raises(service.TrainingConflict, match="exact target version"):
        cohorts.enqueue(
            session,
            version_id=successor.id,
            cohort_id=cohort.id,
            model=MODEL,
            idempotency_key="new-version-old-cohort",
            by="owner",
        )
    with pytest.raises(service.TrainingError, match="unique corrections"):
        cohorts.create_cohort(
            session, name="Duplicate target", version_ids=[row.id, successor.id], by="owner"
        )


def test_cohort_rejects_task_schema_mixing(session: Session) -> None:
    _, root = root_candidate(session)
    first = version(session, root)
    world = source(session, actor="another")
    another = service.create_candidate(
        session,
        world["correction"].id,
        rule="A rule.",
        by="owner",
        snapshot_id=world["snapshot"].id,
        expected_output=EXPECTED,
    )
    second = version(session, another)
    with pytest.raises(service.TrainingError, match="one task and output schema"):
        cohorts.create_cohort(session, name="Mixed", version_ids=[first.id, second.id], by="owner")


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://10.20.30.40:11434"},
        {"base_url": "http://localhost:11434"},
        {"base_url": "http://host.docker.internal:11434"},
        {"name": "remote:cloud"},
        {"seed": True},
        {"temperature": float("nan")},
        {"digest": "unknown"},
        {"tier": "cloud"},
    ],
)
def test_private_comparisons_refuse_unsafe_configuration_without_model_calls(
    session: Session, change
) -> None:  # type: ignore[no-untyped-def]
    _, root = root_candidate(session)
    row = version(session, root)
    cohort = cohorts.create_cohort(session, name="Private", version_ids=[row.id], by="owner")
    cohorts.attest_cohort(session, cohort.id, by="owner")
    with pytest.raises(service.TrainingError):
        cohorts.enqueue(
            session,
            version_id=row.id,
            cohort_id=cohort.id,
            model={**MODEL, **change},
            idempotency_key="unsafe",
            by="owner",
        )
    assert session.scalar(select(func.count()).select_from(ComparisonJob)) == 0


def test_enqueue_is_idempotent_and_binds_all_generation_settings(session: Session) -> None:
    world, _, row, cohort, job = prepared(session)
    assert (
        cohorts.enqueue(
            session,
            version_id=row.id,
            cohort_id=cohort.id,
            model=MODEL,
            idempotency_key="once",
            by="owner",
        ).id
        == job.id
    )
    with pytest.raises(service.TrainingConflict, match="idempotency key"):
        cohorts.enqueue(
            session,
            version_id=row.id,
            cohort_id=cohort.id,
            model={**MODEL, "seed": 42},
            idempotency_key="once",
            by="owner",
        )
    frozen = cohorts.prepare_execution(session, job.id)
    assert frozen["target_case_id"] == str(world["correction"].id)
    assert frozen["private"] is True and frozen["split"] == "tuning"
    assert frozen["readiness_established"] is False and frozen["router_attempt_limit"] == 2
    assert frozen["baseline_system"] == world["snapshot"].system
    assert frozen["hashes"]["implementation"]


def test_durable_claims_never_repeat_unknown_inference(session: Session) -> None:
    _, _, _, _, job = prepared(session)
    case_id = job.contract["cases"][0]["id"]
    claim = cohorts.begin_attempt(session, job.id, side="baseline", case_id=case_id, repetition=1)
    assert claim["state"] == "new"
    assert (
        cohorts.begin_attempt(session, job.id, side="baseline", case_id=case_id, repetition=1)[
            "state"
        ]
        == "unknown"
    )
    control.pause(session, "owner")
    cohorts.record_attempt_observation(
        session,
        job.id,
        attempt_key=claim["attempt_key"],
        call_index=1,
        phase="finished",
        observation={"known_tokens": 9, "api_cost_usd": "0"},
    )
    result = {
        "side": "baseline",
        "case_id": case_id,
        "repetition": 1,
        "status": "error",
        "error": "unknown",
    }
    cohorts.finish_attempt(session, job.id, attempt_key=claim["attempt_key"], result=result)
    cohorts.finish_job(session, job.id, {"valid": False, "verdict": "invalid", "results": [result]})
    assert cohorts.comparison_status(session, job)[0] == "failed"


def test_valid_summary_requires_all_durable_results_and_reads_revalidate_source(
    session: Session,
) -> None:
    world, _, _, _, job = prepared(session)
    with pytest.raises(service.TrainingConflict, match="complete durable attempts"):
        cohorts.finish_job(session, job.id, {"valid": True, "results": []})
    results = completed_results(session, job)
    cohorts.finish_job(
        session, job.id, {"valid": True, "verdict": "no_improvement", "results": results}
    )
    control.pause(session, "owner")
    assert cohorts.comparison_status(session, job)[0] == "completed"
    with session.begin_nested():
        session.execute(
            text(
                "ALTER TABLE training_draft_snapshots "
                "DISABLE TRIGGER training_draft_snapshots_append_only"
            )
        )
        session.execute(
            text("UPDATE training_draft_snapshots SET system='tampered' WHERE id=:id"),
            {"id": world["snapshot"].id},
        )
        session.execute(
            text(
                "ALTER TABLE training_draft_snapshots "
                "ENABLE TRIGGER training_draft_snapshots_append_only"
            )
        )
    session.expire_all()
    with pytest.raises(service.TrainingConflict, match="integrity"):
        cohorts.comparison_status(session, cohorts.get_comparison(session, job.id))


@pytest.mark.parametrize("role,status", [(None, 401), ("staff", 403)])
def test_version_cohort_and_comparison_api_is_owner_only(
    api, session: Session, role, status
) -> None:  # type: ignore[no-untyped-def]
    assert isinstance(api.app, FastAPI)
    if not any(getattr(route, "path", "") == "/v1/training/cohorts" for route in api.app.routes):
        api.app.include_router(routes_comparisons.router(), prefix="/v1")
    if role:
        signin(api, session, role)
    identifier = str(uuid.uuid4())
    for path in (
        f"/training/candidates/{identifier}/versions",
        f"/training/versions/{identifier}",
        "/training/cohorts",
        f"/training/cohorts/{identifier}",
        "/training/comparisons",
        f"/training/comparisons/{identifier}",
    ):
        assert api.get("/v1" + path).status_code == status
    for path, body in (
        (
            f"/training/candidates/{identifier}/versions",
            {
                "parent_id": None,
                "rule": "A rule",
                "system": "A prompt",
                "expected_output": EXPECTED,
            },
        ),
        (f"/training/versions/{identifier}/review", {}),
        (f"/training/versions/{identifier}/reject", {}),
        ("/training/cohorts", {"name": "test", "version_ids": [identifier]}),
        (f"/training/cohorts/{identifier}/attest", {}),
        (
            "/training/comparisons",
            {
                "version_id": identifier,
                "cohort_id": identifier,
                "idempotency_key": "test",
                "model": MODEL,
            },
        ),
    ):
        assert api.post("/v1" + path, json=body).status_code == status


def test_owner_api_freezes_reviewed_private_setup_and_enqueues_promptly(
    api, session: Session
) -> None:  # type: ignore[no-untyped-def]
    signin(api, session)
    assert isinstance(api.app, FastAPI)
    if not any(getattr(route, "path", "") == "/v1/training/cohorts" for route in api.app.routes):
        api.app.include_router(routes_comparisons.router(), prefix="/v1")
    world = source(session)
    created = api.post(
        f"/v1/training/corrections/{world['correction'].id}/candidates",
        json={
            "rule": "Ask for evidence.",
            "snapshot_id": str(world["snapshot"].id),
            "expected_output": EXPECTED,
        },
    )
    assert created.status_code == 200, created.text
    root_id = created.json()["id"]
    row = api.post(
        f"/v1/training/candidates/{root_id}/versions",
        json={
            "parent_id": None,
            "rule": "Ask for evidence.",
            "system": "Prepare a factual reply.",
            "expected_output": EXPECTED,
        },
    )
    assert row.status_code == 200, row.text
    payload = row.json()
    assert payload["schema"] == world["snapshot"].output_schema
    assert payload["status"] == "proposed" and payload["review"] is None
    identifier = payload["id"]
    assert api.post(f"/v1/training/versions/{identifier}/review", json={}).status_code == 200
    cohort = api.post(
        "/v1/training/cohorts", json={"name": "Reviewed case", "version_ids": [identifier]}
    )
    assert cohort.status_code == 200, cohort.text
    cohort_id = cohort.json()["id"]
    assert cohort.json()["private"] is True and cohort.json()["split"] == "tuning"
    control.pause(session, "owner")
    assert api.post(f"/v1/training/cohorts/{cohort_id}/attest", json={}).status_code == 409
    control.resume(session, "owner")
    assert api.post(f"/v1/training/cohorts/{cohort_id}/attest", json={}).status_code == 200
    body = {
        "version_id": identifier,
        "cohort_id": cohort_id,
        "idempotency_key": "one-click",
        "model": MODEL,
    }
    queued = api.post("/v1/training/comparisons", json=body)
    assert queued.status_code == 200, queued.text
    assert queued.headers["cache-control"] == "no-store"
    assert queued.json()["status"] == "queued" and queued.json()["events"] == []
    assert queued.json()["readiness_established"] is False
    assert api.post("/v1/training/comparisons", json=body).json()["id"] == queued.json()["id"]
    control.pause(session, "owner")
    assert api.get(f"/v1/training/comparisons/{queued.json()['id']}").status_code == 200
    assert (
        api.post("/v1/training/comparisons", json={**body, "idempotency_key": "paused"}).status_code
        == 409
    )


def test_unknown_check_branch_settings_are_not_silently_ignored(session: Session) -> None:
    _, root = root_candidate(session)
    with pytest.raises(service.TrainingError, match="unsupported settings"):
        version(
            session,
            root,
            accepted=False,
            checks=[
                {
                    "type": "exact",
                    "fields": ["body"],
                    "unless_expected": {"body": "anything"},
                }
            ],
        )
    assert session.scalar(select(func.count()).select_from(TrainingVersion)) == 0
