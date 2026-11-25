"""Capture immutable draft evidence and owner-proposed cases for testing only."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC
from typing import Any, Literal

import jsonschema
from referencing.exceptions import Unresolvable
from sqlalchemy import and_, literal, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from biz_kernel.approvals.models import Correction, StagedOperation
from biz_kernel.audit.log import record
from biz_kernel.control import service as control
from biz_kernel.db import utcnow, uuid7
from biz_kernel.training.models import DraftSnapshot, TrainingCandidate, TrainingReview
from biz_kernel.workflows.models import SopRun, SopStep

Decision = Literal["accepted_for_testing", "rejected"]
SNAPSHOT_FIELDS = (
    "run_id",
    "step_id",
    "actor",
    "task",
    "prompt_path",
    "system",
    "output_schema",
    "untrusted_text",
    "output",
    "tier",
    "backend",
    "configured_model",
    "model_revision",
)


class TrainingError(ValueError):
    """An invalid training proposal, never an instruction to execute anything."""


class TrainingNotFound(TrainingError):
    pass


class TrainingConflict(TrainingError):
    pass


def _stable(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(_stable(value), allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise TrainingError("training evidence must contain finite JSON values") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_json(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _record_identity(record: DraftSnapshot | TrainingCandidate) -> dict[str, str]:
    return {"id": str(record.id), "at": record.at.astimezone(UTC).isoformat()}


def _candidate_payload(candidate: TrainingCandidate) -> dict[str, Any]:
    return {
        "correction_id": str(candidate.correction_id),
        "created_by": candidate.created_by,
        "rule": candidate.rule,
        "snapshot_id": str(candidate.snapshot_id) if candidate.snapshot_id else None,
        "expected_output": candidate.expected_output,
    }


def _validate_output(output: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise TrainingError("expected model output must be an object")

    def check_references(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in {"$ref", "$dynamicRef"}
                    and isinstance(value, str)
                    and not value.startswith("#")
                ):
                    raise TrainingError("training schemas may reference only their own document")
                check_references(value)
        elif isinstance(node, list):
            for value in node:
                check_references(value)

    check_references(schema)
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
        jsonschema.validate(output, schema)
    except (jsonschema.ValidationError, jsonschema.SchemaError, Unresolvable) as exc:
        # Validation errors can contain private draft text; keep it out of diagnostics.
        raise TrainingError("model output does not match the captured output schema") from exc
    return _json(output)


def capture_draft(
    session: Session,
    *,
    run_id: str,
    step_id: str,
    actor: str,
    task: str,
    prompt_path: str,
    system: str,
    schema: dict[str, Any],
    untrusted_text: str,
    output: dict[str, Any],
    tier: str,
    backend: str,
    configured_model: str | None,
    model_revision: str | None = None,
) -> DraftSnapshot:
    """Save observed draft context once, in the draft ledger transaction.

    No provider metadata is inferred or fetched. Unknown model revisions remain null.
    A resumed draft must agree exactly with its already captured evidence.
    """
    run = session.get(SopRun, run_id)
    if run is None or run.role != actor:
        raise TrainingError("draft evidence must match its workflow actor")
    if (
        tier not in {"local", "cloud"}
        or not isinstance(system, str)
        or not isinstance(untrusted_text, str)
    ):
        raise TrainingError("draft evidence needs its observed tier and text inputs")
    for value, limit in ((run_id, 200), (step_id, 80), (actor, 120), (task, 160), (backend, 240)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise TrainingError("draft evidence has an invalid source identifier")
    if not isinstance(prompt_path, str) or not prompt_path.strip():
        raise TrainingError("draft evidence needs its source prompt path")
    for model_value in (configured_model, model_revision):
        if model_value is not None and (
            not isinstance(model_value, str) or not model_value or len(model_value) > 240
        ):
            raise TrainingError("model evidence must be observed text or null")
    schema, output = _json(schema), _validate_output(output, schema)
    payload = {
        "run_id": run_id,
        "step_id": step_id,
        "actor": actor,
        "task": task,
        "prompt_path": prompt_path,
        "system": system,
        "output_schema": schema,
        "untrusted_text": untrusted_text,
        "output": output,
        "tier": tier,
        "backend": backend,
        "configured_model": configured_model,
        "model_revision": model_revision,
    }
    payload_sha = digest(payload)
    # Serialize a duplicated workflow step before looking up its immutable snapshot.
    key = (
        int.from_bytes(hashlib.sha256(f"training:{run_id}:{step_id}".encode()).digest()[:8], "big")
        >> 1
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    existing = session.scalar(
        select(DraftSnapshot).where(
            DraftSnapshot.run_id == run_id, DraftSnapshot.step_id == step_id
        )
    )
    if existing is not None:
        validate_snapshot(existing)
        if digest({key: getattr(existing, key) for key in SNAPSHOT_FIELDS}) != payload_sha:
            raise TrainingConflict("draft evidence differs from its captured workflow step")
        return existing
    snapshot = DraftSnapshot(
        id=uuid7(),
        at=utcnow(),
        **payload,
        system_sha256=_text_digest(system),
        schema_sha256=digest(schema),
        input_sha256=_text_digest(untrusted_text),
        output_sha256=digest(output),
        payload_sha256="",
    )
    snapshot.payload_sha256 = digest({**payload, **_record_identity(snapshot)})
    session.add(snapshot)
    session.flush()
    record(
        session,
        actor,
        "training.snapshot",
        str(snapshot.id),
        run_id=run_id,
        step_id=step_id,
        payload_sha256=snapshot.payload_sha256,
    )
    return snapshot


def validate_snapshot(snapshot: DraftSnapshot) -> None:
    """Verify persisted contents rather than trusting only their recorded hash label."""
    try:
        expected = {
            "system_sha256": _text_digest(snapshot.system),
            "schema_sha256": digest(snapshot.output_schema),
            "input_sha256": _text_digest(snapshot.untrusted_text),
            "output_sha256": digest(snapshot.output),
            "payload_sha256": digest(
                {
                    **{key: getattr(snapshot, key) for key in SNAPSHOT_FIELDS},
                    **_record_identity(snapshot),
                }
            ),
        }
        if any(getattr(snapshot, key) != value for key, value in expected.items()):
            raise TrainingConflict("training evidence integrity check failed")
    except (TrainingError, TypeError, AttributeError) as exc:
        raise TrainingConflict("training evidence integrity check failed") from exc


def correction(session: Session, correction_id: uuid.UUID, *, lock: bool = False) -> Correction:
    row = session.get(Correction, correction_id, with_for_update=lock)
    if row is None:
        raise TrainingNotFound("no such correction")
    return row


def eligible_snapshots(session: Session, source: Correction) -> list[DraftSnapshot]:
    """Only draft snapshots from the edited operation's actor and actual workflow."""
    op = session.get(StagedOperation, source.op_id)
    if op is None or op.actor != source.actor:
        return []
    op_id = str(source.op_id)
    linked_steps = select(SopStep.run_id).where(
        or_(
            and_(SopStep.step_id.like("%:staged"), SopStep.output.contains({"op_id": op_id})),
            and_(SopStep.step_id.like("%:request"), SopStep.output == literal(op_id, JSONB)),
        )
    )
    linked_runs = select(SopRun.id).where(
        SopRun.role == source.actor,
        or_(SopRun.waiting_op_id == op_id, SopRun.id.in_(linked_steps)),
    )
    snapshots = list(
        session.scalars(
            select(DraftSnapshot)
            .where(
                DraftSnapshot.run_id.in_(linked_runs),
                DraftSnapshot.actor == source.actor,
                DraftSnapshot.at <= source.at,
            )
            .order_by(DraftSnapshot.at, DraftSnapshot.id)
        )
    )
    for snapshot in snapshots:
        validate_snapshot(snapshot)
    return snapshots


def candidate_for_correction(
    session: Session, correction_id: uuid.UUID
) -> TrainingCandidate | None:
    return session.scalar(
        select(TrainingCandidate).where(TrainingCandidate.correction_id == correction_id)
    )


def create_candidate(
    session: Session,
    correction_id: uuid.UUID,
    *,
    rule: str,
    by: str,
    snapshot_id: uuid.UUID | None = None,
    expected_output: dict[str, Any] | None = None,
) -> TrainingCandidate:
    """Propose a rule and optional source-backed case. Never generate or activate one."""
    if not isinstance(rule, str) or not rule.strip() or len(rule) > 8000:
        raise TrainingError("rule must contain between 1 and 8000 characters")
    source = correction(session, correction_id, lock=True)
    snapshot = None
    if snapshot_id is not None:
        snapshot = next(
            (row for row in eligible_snapshots(session, source) if row.id == snapshot_id), None
        )
        if snapshot is None:
            raise TrainingError("snapshot does not belong to this correction's workflow and actor")
    if expected_output is not None:
        if not isinstance(expected_output, dict):
            raise TrainingError("expected model output must be an object")
        expected_output = (
            _validate_output(expected_output, snapshot.output_schema)
            if snapshot is not None
            else _json(expected_output)
        )
    payload = {
        "correction_id": str(correction_id),
        "created_by": by,
        "rule": rule.strip(),
        "snapshot_id": str(snapshot_id) if snapshot_id else None,
        "expected_output": expected_output,
    }
    payload_sha = digest(payload)
    existing = candidate_for_correction(session, correction_id)
    if existing is not None:
        validate_candidate(session, existing)
        if digest(_candidate_payload(existing)) != payload_sha:
            raise TrainingConflict("this correction already has a different immutable candidate")
        return existing
    candidate = TrainingCandidate(
        id=uuid7(),
        at=utcnow(),
        correction_id=correction_id,
        snapshot_id=snapshot_id,
        rule=payload["rule"],
        expected_output=expected_output,
        created_by=by,
        payload_sha256="",
    )
    candidate.payload_sha256 = digest({**payload, **_record_identity(candidate)})
    session.add(candidate)
    session.flush()
    record(
        session,
        by,
        "training.candidate",
        str(candidate.id),
        event="proposed",
        correction_id=str(correction_id),
        snapshot_id=str(snapshot_id) if snapshot_id else None,
        payload_sha256=candidate.payload_sha256,
    )
    return candidate


def get_candidate(
    session: Session, candidate_id: uuid.UUID, *, lock: bool = False
) -> TrainingCandidate:
    candidate = session.get(TrainingCandidate, candidate_id, with_for_update=lock)
    if candidate is None:
        raise TrainingNotFound("no such training candidate")
    return candidate


def review_for(session: Session, candidate_id: uuid.UUID) -> TrainingReview | None:
    return session.scalar(select(TrainingReview).where(TrainingReview.candidate_id == candidate_id))


def incomplete_reasons(candidate: TrainingCandidate) -> list[str]:
    reasons = []
    if candidate.snapshot_id is None:
        reasons.append("No captured model draft is linked to this correction.")
    if candidate.expected_output is None:
        reasons.append("Expected model output has not been supplied and schema-validated.")
    return reasons


def validate_candidate(session: Session, candidate: TrainingCandidate) -> None:
    expected = digest({**_candidate_payload(candidate), **_record_identity(candidate)})
    if expected != candidate.payload_sha256:
        raise TrainingConflict("training evidence integrity check failed")
    if candidate.snapshot_id is not None:
        source = correction(session, candidate.correction_id)
        snapshot = next(
            (
                item
                for item in eligible_snapshots(session, source)
                if item.id == candidate.snapshot_id
            ),
            None,
        )
        if snapshot is None:
            raise TrainingConflict("training evidence integrity check failed")
        if candidate.expected_output is not None:
            try:
                _validate_output(candidate.expected_output, snapshot.output_schema)
            except TrainingError as exc:
                raise TrainingConflict("training evidence integrity check failed") from exc


def decide(
    session: Session, candidate_id: uuid.UUID, *, decision: Decision, by: str, reason: str = ""
) -> TrainingReview:
    """Owner review permits testing only. It never stages an executable operation."""
    if decision not in {"accepted_for_testing", "rejected"}:
        raise TrainingError("unknown training review decision")
    if not isinstance(reason, str) or len(reason) > 2000:
        raise TrainingError("review reason must contain at most 2000 characters")
    candidate = get_candidate(session, candidate_id, lock=True)
    validate_candidate(session, candidate)
    if decision == "accepted_for_testing":
        control.admit(session, "resume before accepting a training candidate")
    existing = review_for(session, candidate_id)
    if existing is not None:
        if (existing.decision, existing.by, existing.reason) != (decision, by, reason.strip()):
            raise TrainingConflict("this candidate already has a different immutable review")
        return existing
    if decision == "accepted_for_testing" and incomplete_reasons(candidate):
        raise TrainingConflict("incomplete candidates cannot be accepted for testing")
    review = TrainingReview(
        candidate_id=candidate_id, decision=decision, by=by, reason=reason.strip()
    )
    session.add(review)
    session.flush()
    record(
        session,
        by,
        "training.review",
        str(candidate_id),
        decision=decision,
        review_id=str(review.id),
        readiness_established=False,
    )
    return review


def regression_case(session: Session, candidate: TrainingCandidate) -> dict[str, Any] | None:
    validate_candidate(session, candidate)
    if incomplete_reasons(candidate):
        return None
    snapshot = session.get(DraftSnapshot, candidate.snapshot_id)
    if snapshot is None:
        return None
    return {
        "version": 1,
        "name": f"correction-{candidate.correction_id}",
        "task": snapshot.task,
        "system": snapshot.system,
        "schema": snapshot.output_schema,
        "untrusted": [snapshot.untrusted_text],
        "expected": candidate.expected_output,
        "checks": [
            {"type": "json_schema"},
            {"type": "exact", "fields": sorted((candidate.expected_output or {}).keys())},
        ],
        "proposed_rule": candidate.rule,
        "source": {
            "candidate_id": str(candidate.id),
            "candidate_payload_sha256": candidate.payload_sha256,
            "correction_id": str(candidate.correction_id),
            "snapshot_id": str(snapshot.id),
            "payload_sha256": snapshot.payload_sha256,
            "schema_sha256": snapshot.schema_sha256,
            "input_sha256": snapshot.input_sha256,
            "output_sha256": snapshot.output_sha256,
            "prompt_path": snapshot.prompt_path,
            "model": {
                "tier": snapshot.tier,
                "backend": snapshot.backend,
                "configured_model": snapshot.configured_model,
                "model_revision": snapshot.model_revision,
            },
        },
        "split": "tuning",
        "private": True,
        "owner_reviewed": False,
        "readiness_established": False,
    }
