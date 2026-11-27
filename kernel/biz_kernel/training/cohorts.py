"""Frozen private tuning cohorts and durable, local-only comparison requests."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.control import service as control
from biz_kernel.db import utcnow, uuid7
from biz_kernel.models.local_ollama import BACKEND, literal_loopback_url
from biz_kernel.training import service as training
from biz_kernel.training.models import (
    ComparisonEvent,
    ComparisonJob,
    TrainingCohort,
    TrainingCohortReview,
    TrainingVersion,
)

SCORER_VERSION = "paired-training-v1"


def _fields(row: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, uuid.UUID) else value
        for key in names
        for value in (getattr(row, key),)
    }


def _seal(row: Any, fields: dict[str, Any]) -> str:
    return training.digest({**fields, **training._record_identity(row)})


def _lock(session: Session, value: str) -> None:
    key = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") >> 1
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def member(session: Session, version: TrainingVersion) -> dict[str, Any]:
    snapshot = training.validate_version(session, version)
    review = training.version_review_for(session, version)
    if (
        snapshot is None
        or training.version_incomplete_reasons(version, snapshot)
        or review is None
        or review.decision != "accepted_for_testing"
    ):
        raise training.TrainingConflict(
            "every cohort version needs its own source-backed testing review"
        )
    root = training.get_candidate(session, version.candidate_id)
    return {
        "case_id": str(root.correction_id),
        "version_id": str(version.id),
        "version_sha256": version.payload_sha256,
        "review_id": str(review.id),
        "review_sha256": review.payload_sha256,
        "snapshot_id": str(snapshot.id),
        "source_sha256": snapshot.payload_sha256,
        "task": snapshot.task,
        "schema": snapshot.output_schema,
        "schema_sha256": snapshot.schema_sha256,
        "original_system": snapshot.system,
        "messages": [],
        "untrusted": [snapshot.untrusted_text],
        "expected": version.expected_output,
        "checks": version.checks,
    }


def _cohort_fields(row: TrainingCohort) -> dict[str, Any]:
    return _fields(row, ("name", "task", "schema_sha256", "members", "created_by"))


def get_cohort(session: Session, cohort_id: uuid.UUID, *, lock: bool = False) -> TrainingCohort:
    row = session.get(TrainingCohort, cohort_id, with_for_update=lock)
    if row is None:
        raise training.TrainingNotFound("no such private training cohort")
    return row


def validate_cohort(session: Session, row: TrainingCohort) -> None:
    if row.payload_sha256 != _seal(row, _cohort_fields(row)) or not row.members:
        raise training.TrainingConflict("private cohort integrity check failed")
    seen = set()
    for frozen in row.members:
        try:
            version = training.get_version(session, uuid.UUID(frozen["version_id"]))
            current = member(session, version)
        except (ValueError, KeyError, TypeError) as exc:
            raise training.TrainingConflict("private cohort source evidence is invalid") from exc
        if (
            training.digest(frozen) != training.digest(current)
            or current["case_id"] in seen
            or current["task"] != row.task
            or current["schema_sha256"] != row.schema_sha256
        ):
            raise training.TrainingConflict("private cohort source evidence changed")
        seen.add(current["case_id"])


def create_cohort(
    session: Session, *, name: str, version_ids: list[uuid.UUID], by: str
) -> TrainingCohort:
    if not name.strip() or len(name) > 200 or not 1 <= len(version_ids) <= 100:
        raise training.TrainingError("a named cohort needs between 1 and 100 reviewed cases")
    if len(set(version_ids)) != len(version_ids):
        raise training.TrainingError("cohort versions must be distinct")
    members = [
        member(session, training.get_version(session, identifier)) for identifier in version_ids
    ]
    members.sort(key=lambda item: item["case_id"])
    if (
        len({item["case_id"] for item in members}) != len(members)
        or len({item["task"] for item in members}) != 1
        or len({item["schema_sha256"] for item in members}) != 1
    ):
        raise training.TrainingError(
            "a cohort needs unique corrections sharing one task and output schema"
        )
    fields = {
        "name": name.strip(),
        "task": members[0]["task"],
        "schema_sha256": members[0]["schema_sha256"],
        "members": members,
        "created_by": by,
    }
    _lock(session, "training-cohort:" + training.digest(fields))
    for old in session.scalars(
        select(TrainingCohort).where(
            TrainingCohort.created_by == by, TrainingCohort.name == name.strip()
        )
    ):
        validate_cohort(session, old)
        if training.digest(_cohort_fields(old)) == training.digest(fields):
            return old
    row = TrainingCohort(id=uuid7(), at=utcnow(), **fields, payload_sha256="")
    row.payload_sha256 = _seal(row, fields)
    session.add(row)
    session.flush()
    record(
        session,
        by,
        "training.cohort",
        str(row.id),
        payload_sha256=row.payload_sha256,
        case_count=len(members),
        purpose="private_tuning",
    )
    return row


def cohort_review_for(session: Session, row: TrainingCohort) -> TrainingCohortReview | None:
    review = session.scalar(
        select(TrainingCohortReview).where(TrainingCohortReview.cohort_id == row.id)
    )
    if review is not None:
        fields = _fields(review, ("cohort_id", "cohort_sha256", "by", "reason"))
        if review.cohort_sha256 != row.payload_sha256 or review.payload_sha256 != _seal(
            review, fields
        ):
            raise training.TrainingConflict("private cohort attestation changed")
    return review


def attest_cohort(
    session: Session, cohort_id: uuid.UUID, *, by: str, reason: str = ""
) -> TrainingCohortReview:
    row = get_cohort(session, cohort_id, lock=True)
    validate_cohort(session, row)
    if len(reason) > 2000:
        raise training.TrainingError("attestation reason is too long")
    control.admit(session, "resume before attesting a private tuning cohort")
    old = cohort_review_for(session, row)
    if old is not None:
        if (old.by, old.reason) != (by, reason.strip()):
            raise training.TrainingConflict(
                "this cohort already has a different immutable attestation"
            )
        return old
    review = TrainingCohortReview(
        id=uuid7(),
        at=utcnow(),
        cohort_id=row.id,
        cohort_sha256=row.payload_sha256,
        by=by,
        reason=reason.strip(),
        payload_sha256="",
    )
    review.payload_sha256 = _seal(
        review, _fields(review, ("cohort_id", "cohort_sha256", "by", "reason"))
    )
    session.add(review)
    session.flush()
    record(
        session,
        by,
        "training.cohort_review",
        str(row.id),
        review_id=str(review.id),
        payload_sha256=review.payload_sha256,
        purpose="private_tuning",
        readiness_established=False,
    )
    return review


def model_config(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "name",
        "digest",
        "ollama_version",
        "backend_version",
        "base_url",
        "temperature",
        "seed",
        "num_predict",
        "timeout_seconds",
        "repetitions",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise training.TrainingError("only the pinned local Ollama configuration is supported")
    out = {
        "base_url": "http://127.0.0.1:11434",
        "temperature": 0.1,
        "seed": 0,
        "num_predict": 2048,
        "timeout_seconds": 90,
        "repetitions": 1,
        **value,
    }
    for key in ("name", "digest", "ollama_version", "backend_version"):
        if not isinstance(out.get(key), str) or not out[key] or len(out[key]) > 240:
            raise training.TrainingError(
                "the exact installed model and backend revisions are required"
            )
    if (
        not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", out["digest"])
        or "cloud" in out["name"].lower()
    ):
        raise training.TrainingError("a noncloud local model and exact SHA-256 digest are required")
    try:
        out["base_url"] = literal_loopback_url(out["base_url"])
    except ValueError as exc:
        raise training.TrainingError(
            "comparison inference must use literal appliance loopback"
        ) from exc
    for key, minimum, maximum in (("temperature", 0, 2), ("timeout_seconds", 0.001, 600)):
        number = out[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, int | float)
            or not math.isfinite(number)
            or not minimum <= number <= maximum
        ):
            raise training.TrainingError(
                "generation settings and timeouts must be finite and bounded"
            )
    for key, minimum, maximum in (
        ("seed", -(2**31), 2**31 - 1),
        ("num_predict", 1, 8192),
        ("repetitions", 1, 10),
    ):
        number = out[key]
        if type(number) is not int or not minimum <= number <= maximum:
            raise training.TrainingError(
                "seed, token limit and repetitions must be bounded integers"
            )
    return training._json({"backend": BACKEND, **out})


def _contract(
    session: Session,
    version_id: uuid.UUID,
    cohort_id: uuid.UUID,
    model: dict[str, Any],
    implementation: dict[str, str],
) -> dict[str, Any]:
    version = training.get_version(session, version_id)
    snapshot = training.validate_version(session, version)
    version_review = training.version_review_for(session, version)
    cohort = get_cohort(session, cohort_id)
    validate_cohort(session, cohort)
    cohort_review = cohort_review_for(session, cohort)
    if (
        snapshot is None
        or version_review is None
        or version_review.decision != "accepted_for_testing"
        or cohort_review is None
        or training.version_incomplete_reasons(version, snapshot)
    ):
        raise training.TrainingConflict(
            "exact version and private cohort need their own owner testing review"
        )
    root = training.get_candidate(session, version.candidate_id)
    target = str(root.correction_id)
    if not any(
        item["version_id"] == str(version.id) and item["case_id"] == target
        for item in cohort.members
    ):
        raise training.TrainingConflict(
            "the cohort must freeze this exact target version and expected outcome"
        )
    if cohort.task != snapshot.task or cohort.schema_sha256 != snapshot.schema_sha256:
        raise training.TrainingConflict("version and cohort task/schema differ")
    return training._json(
        {
            "purpose": "private_training_comparison",
            "split": "tuning",
            "private": True,
            "readiness_established": False,
            "version_id": str(version.id),
            "cohort_id": str(cohort.id),
            "target_case_id": target,
            "task": snapshot.task,
            "schema": snapshot.output_schema,
            "baseline_system": snapshot.system,
            "candidate_system": version.system,
            "cases": [
                {
                    "id": item["case_id"],
                    **{key: item[key] for key in ("messages", "untrusted", "expected", "checks")},
                }
                for item in cohort.members
            ],
            "model": model,
            "router_attempt_limit": 2,
            "hashes": {
                "version": version.payload_sha256,
                "version_review": version_review.payload_sha256,
                "cohort": cohort.payload_sha256,
                "cohort_review": cohort_review.payload_sha256,
                "baseline_prompt": training._text_digest(snapshot.system),
                "candidate_prompt": training._text_digest(version.system),
                "schema": snapshot.schema_sha256,
                "scorer": SCORER_VERSION,
                "implementation": implementation,
            },
        }
    )


def _job_fields(row: ComparisonJob) -> dict[str, Any]:
    return _fields(
        row,
        (
            "version_id",
            "cohort_id",
            "version_review_id",
            "cohort_review_id",
            "created_by",
            "idempotency_key",
            "contract",
            "request_sha256",
        ),
    )


def get_comparison(session: Session, job_id: uuid.UUID, *, lock: bool = False) -> ComparisonJob:
    row = session.get(ComparisonJob, job_id, with_for_update=lock)
    if row is None:
        raise training.TrainingNotFound("no such comparison request")
    if row.payload_sha256 != _seal(row, _job_fields(row)):
        raise training.TrainingConflict("comparison request integrity check failed")
    return row


def enqueue(
    session: Session,
    *,
    version_id: uuid.UUID,
    cohort_id: uuid.UUID,
    model: dict[str, Any],
    idempotency_key: str,
    by: str,
) -> ComparisonJob:
    if not idempotency_key or len(idempotency_key) > 120:
        raise training.TrainingError("comparison requests require a stable idempotency key")
    from biz_kernel.training.comparison import implementation_pins

    normalized = model_config(model)
    contract = _contract(session, version_id, cohort_id, normalized, implementation_pins())
    control.admit(session, "resume before enqueueing a local comparison")
    _lock(session, f"training-comparison:{by}:{idempotency_key}")
    request_sha = training.digest(contract)
    old = session.scalar(
        select(ComparisonJob).where(
            ComparisonJob.created_by == by, ComparisonJob.idempotency_key == idempotency_key
        )
    )
    if old is not None:
        get_comparison(session, old.id)
        if old.request_sha256 != request_sha:
            raise training.TrainingConflict(
                "idempotency key already names a different frozen comparison"
            )
        return old
    version = training.get_version(session, version_id)
    cohort = get_cohort(session, cohort_id)
    vr, cr = training.version_review_for(session, version), cohort_review_for(session, cohort)
    assert vr is not None and cr is not None
    row = ComparisonJob(
        id=uuid7(),
        at=utcnow(),
        version_id=version_id,
        cohort_id=cohort_id,
        version_review_id=vr.id,
        cohort_review_id=cr.id,
        created_by=by,
        idempotency_key=idempotency_key,
        contract=contract,
        request_sha256=request_sha,
        payload_sha256="",
    )
    row.payload_sha256 = _seal(row, _job_fields(row))
    session.add(row)
    session.flush()
    record(
        session,
        by,
        "training.comparison",
        str(row.id),
        event="queued",
        payload_sha256=row.payload_sha256,
        readiness_established=False,
    )
    return row


def validate_comparison_sources(session: Session, job: ComparisonJob) -> None:
    rebuilt = _contract(
        session,
        job.version_id,
        job.cohort_id,
        job.contract["model"],
        job.contract["hashes"]["implementation"],
    )
    if (
        training.digest(rebuilt) != job.request_sha256
        or training.digest(job.contract) != job.request_sha256
    ):
        raise training.TrainingConflict("comparison source or review evidence changed")


def prepare_execution(session: Session, job_id: uuid.UUID) -> dict[str, Any]:
    job = get_comparison(session, job_id)
    validate_comparison_sources(session, job)
    control.admit(session, "resume before local comparison inference")
    return training._json({"job_id": str(job.id), **job.contract})


def events(session: Session, job_id: uuid.UUID) -> list[ComparisonEvent]:
    rows = list(
        session.scalars(
            select(ComparisonEvent)
            .where(ComparisonEvent.job_id == job_id)
            .order_by(ComparisonEvent.at, ComparisonEvent.id)
        )
    )
    for row in rows:
        if row.payload_sha256 != _seal(
            row, _fields(row, ("job_id", "kind", "event_key", "payload"))
        ):
            raise training.TrainingConflict("comparison event integrity check failed")
    return rows


def append_event(
    session: Session, job_id: uuid.UUID, *, kind: str, key: str, payload: dict[str, Any]
) -> ComparisonEvent:
    job = get_comparison(session, job_id, lock=True)
    normalized = training._json(payload)
    old = session.scalar(
        select(ComparisonEvent).where(
            ComparisonEvent.job_id == job.id, ComparisonEvent.event_key == key
        )
    )
    if old is not None:
        if old.kind != kind or training.digest(old.payload) != training.digest(normalized):
            raise training.TrainingConflict("comparison evidence is immutable")
        events(session, job_id)
        return old
    row = ComparisonEvent(
        id=uuid7(),
        at=utcnow(),
        job_id=job.id,
        kind=kind,
        event_key=key,
        payload=normalized,
        payload_sha256="",
    )
    row.payload_sha256 = _seal(row, _fields(row, ("job_id", "kind", "event_key", "payload")))
    session.add(row)
    session.flush()
    return row


def start_job(session: Session, job_id: uuid.UUID) -> bool:
    prepare_execution(session, job_id)
    if any(row.kind == "finished" for row in events(session, job_id)):
        return False
    append_event(session, job_id, kind="started", key="job.started", payload={"started": True})
    return True


def _attempt(job: ComparisonJob, *, side: str, case_id: str, repetition: int) -> str:
    if (
        side not in {"baseline", "candidate"}
        or type(repetition) is not int
        or not 1 <= repetition <= job.contract["model"]["repetitions"]
        or case_id not in {case["id"] for case in job.contract["cases"]}
    ):
        raise training.TrainingError("attempt does not belong to this frozen comparison")
    return f"{side}:{case_id}:{repetition}"


def begin_attempt(
    session: Session, job_id: uuid.UUID, *, side: str, case_id: str, repetition: int
) -> dict[str, Any]:
    prepare_execution(session, job_id)
    job = get_comparison(session, job_id, lock=True)
    key = _attempt(job, side=side, case_id=case_id, repetition=repetition)
    rows = {row.event_key: row for row in events(session, job_id)}
    if f"attempt:{key}:finished" in rows:
        return {
            "state": "completed",
            "attempt_key": key,
            "result": rows[f"attempt:{key}:finished"].payload,
        }
    if f"attempt:{key}:started" in rows or "job.finished" in rows:
        return {"state": "unknown", "attempt_key": key}
    append_event(
        session,
        job_id,
        kind="attempt_started",
        key=f"attempt:{key}:started",
        payload={"side": side, "case_id": case_id, "repetition": repetition},
    )
    return {"state": "new", "attempt_key": key}


def finish_attempt(
    session: Session, job_id: uuid.UUID, *, attempt_key: str, result: dict[str, Any]
) -> ComparisonEvent:
    if not session.scalar(
        select(ComparisonEvent.id).where(
            ComparisonEvent.job_id == job_id,
            ComparisonEvent.event_key == f"attempt:{attempt_key}:started",
        )
    ):
        raise training.TrainingConflict("attempt was not durably started")
    return append_event(
        session,
        job_id,
        kind="attempt_finished",
        key=f"attempt:{attempt_key}:finished",
        payload=result,
    )


def record_attempt_observation(
    session: Session,
    job_id: uuid.UUID,
    *,
    attempt_key: str,
    call_index: int,
    phase: str,
    observation: dict[str, Any],
) -> ComparisonEvent:
    if type(call_index) is not int or call_index < 1 or phase not in {"started", "finished"}:
        raise training.TrainingError("invalid model observation")
    if not session.scalar(
        select(ComparisonEvent.id).where(
            ComparisonEvent.job_id == job_id,
            ComparisonEvent.event_key == f"attempt:{attempt_key}:started",
        )
    ):
        raise training.TrainingConflict("attempt was not durably started")
    return append_event(
        session,
        job_id,
        kind="model_observation",
        key=f"observation:{attempt_key}:{call_index}:{phase}",
        payload={
            "attempt_key": attempt_key,
            "call_index": call_index,
            "phase": phase,
            "observation": observation,
        },
    )


def finish_job(session: Session, job_id: uuid.UUID, summary: dict[str, Any]) -> ComparisonEvent:
    if summary.get("valid", summary.get("comparison_valid")) is True:
        job = get_comparison(session, job_id)
        validate_comparison_sources(session, job)
        observed = [
            row.payload for row in events(session, job_id) if row.kind == "attempt_finished"
        ]
        required = {
            (side, case["id"], repetition)
            for side in ("baseline", "candidate")
            for case in job.contract["cases"]
            for repetition in range(1, job.contract["model"]["repetitions"] + 1)
        }
        keys = {
            (item.get("side"), item.get("case_id"), item.get("repetition")) for item in observed
        }
        if (
            keys != required
            or len(observed) != len(required)
            or any(item.get("status") != "completed" or item.get("error") for item in observed)
            or sorted(training.digest(row) for row in summary.get("results", []))
            != sorted(training.digest(row) for row in observed)
        ):
            raise training.TrainingConflict(
                "comparison summary does not match its complete durable attempts"
            )
    return append_event(session, job_id, kind="finished", key="job.finished", payload=summary)


def pending_comparisons(session: Session, limit: int = 10) -> list[uuid.UUID]:
    finished = select(ComparisonEvent.job_id).where(ComparisonEvent.kind == "finished")
    return list(
        session.scalars(
            select(ComparisonJob.id)
            .where(ComparisonJob.id.not_in(finished))
            .order_by(ComparisonJob.at)
            .limit(limit)
        )
    )


def comparison_status(
    session: Session, job: ComparisonJob
) -> tuple[str, list[ComparisonEvent], dict[str, Any] | None]:
    validate_comparison_sources(session, job)
    rows = events(session, job.id)
    final = next((row.payload for row in rows if row.kind == "finished"), None)
    if final is not None:
        status = (
            "completed"
            if final.get("comparison_valid", final.get("valid")) is True
            else ("unknown" if final.get("status") == "unknown" else "failed")
        )
    else:
        status = "running" if any(row.kind == "started" for row in rows) else "queued"
    return status, rows, final
