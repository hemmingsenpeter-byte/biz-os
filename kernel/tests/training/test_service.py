"""Evidence integrity and idempotent owner proposals, never automatic deployment."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from biz_kernel import control, db
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import AutonomyLevel, Correction, StagedOperation, Suggestion
from biz_kernel.audit.log import AuditEntry
from biz_kernel.training import service
from biz_kernel.training.models import DraftSnapshot, TrainingCandidate
from biz_kernel.workflows.models import SopStep
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .conftest import EXPECTED, source


def propose(session: Session, world):  # type: ignore[no-untyped-def]
    return service.create_candidate(
        session,
        world["correction"].id,
        rule="Ask for evidence before making a commitment.",
        by="owner",
        snapshot_id=world["snapshot"].id,
        expected_output=EXPECTED,
    )


def test_draft_capture_is_idempotent_and_freezes_actual_unknowns(session: Session) -> None:
    world = source(session)
    captured = world["snapshot"]
    repeat = service.capture_draft(session, **world["capture"])
    assert repeat.id == captured.id
    assert repeat.configured_model is None and repeat.model_revision is None
    assert session.scalar(select(func.count()).select_from(DraftSnapshot)) == 1
    changed = {**world["capture"], "output": {"body": "A different answer."}}
    with pytest.raises(service.TrainingConflict):
        service.capture_draft(session, **changed)
    assert session.scalar(select(func.count()).select_from(DraftSnapshot)) == 1


def test_candidate_is_idempotent_but_changed_payload_conflicts(session: Session) -> None:
    world = source(session)
    candidate = propose(session, world)
    assert propose(session, world).id == candidate.id
    assert session.scalar(select(func.count()).select_from(TrainingCandidate)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.kind == "training.candidate")
        )
        == 1
    )
    with pytest.raises(service.TrainingConflict):
        service.create_candidate(
            session,
            world["correction"].id,
            rule="A different instruction.",
            by="owner",
            snapshot_id=world["snapshot"].id,
            expected_output=EXPECTED,
        )


def test_selection_requires_the_actual_workflow_actor_and_earlier_draft(session: Session) -> None:
    first, other = source(session), source(session)
    foreign = source(session, actor="other")
    for wrong in (other["snapshot"], foreign["snapshot"]):
        with pytest.raises(service.TrainingError, match="workflow and actor"):
            service.create_candidate(
                session,
                first["correction"].id,
                rule="A rule.",
                by="owner",
                snapshot_id=wrong.id,
                expected_output=EXPECTED,
            )
    # A later draft in the same run is not the source of the earlier correction.
    later = service.capture_draft(session, **{**first["capture"], "step_id": "later"})
    assert later.at > first["correction"].at
    assert later.id not in {
        row.id for row in service.eligible_snapshots(session, first["correction"])
    }
    with pytest.raises(service.TrainingError, match="workflow and actor"):
        service.create_candidate(
            session,
            first["correction"].id,
            rule="A rule.",
            by="owner",
            snapshot_id=later.id,
            expected_output=EXPECTED,
        )


def test_completed_operation_uses_its_staged_step_link(session: Session) -> None:
    world = source(session)
    world["run"].waiting_op_id = None
    session.add(
        SopStep(
            run_id=world["run"].id, step_id="send:staged", output={"op_id": str(world["op"].id)}
        )
    )
    session.flush()
    assert [row.id for row in service.eligible_snapshots(session, world["correction"])] == [
        world["snapshot"].id
    ]


def test_invalid_expected_output_never_creates_a_candidate(session: Session) -> None:
    world = source(session)
    with pytest.raises(service.TrainingError, match="captured output schema") as caught:
        service.create_candidate(
            session,
            world["correction"].id,
            rule="A rule.",
            by="owner",
            snapshot_id=world["snapshot"].id,
            expected_output={"body": {"private": "do not echo"}},
        )
    assert "do not echo" not in str(caught.value)
    assert session.scalar(select(func.count()).select_from(TrainingCandidate)) == 0


def test_schema_validation_never_fetches_remote_references(session: Session) -> None:
    world = source(session)
    with pytest.raises(service.TrainingError, match="own document"):
        service.capture_draft(
            session,
            **{
                **world["capture"],
                "step_id": "remote",
                "schema": {"$ref": "https://example.invalid/private-schema"},
            },
        )


def test_capture_preserves_the_schema_declared_dialect(session: Session) -> None:
    world = source(session)
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "number", "minimum": 0, "exclusiveMinimum": True}},
    }
    captured = service.capture_draft(
        session,
        **{**world["capture"], "step_id": "draft4", "schema": schema, "output": {"value": 1}},
    )
    assert captured.output == {"value": 1}
    with pytest.raises(service.TrainingError, match="captured output schema"):
        service.capture_draft(
            session,
            **{**world["capture"], "step_id": "invalid4", "schema": schema, "output": {"value": 0}},
        )


def test_legacy_is_explicitly_incomplete_and_can_only_be_rejected(session: Session) -> None:
    world = source(session, snapshot=False)
    candidate = service.create_candidate(
        session, world["correction"].id, rule="A proposed rule.", by="owner"
    )
    assert len(service.incomplete_reasons(candidate)) == 2
    assert service.regression_case(session, candidate) is None
    with pytest.raises(service.TrainingConflict, match="incomplete"):
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
    control.pause(session, "owner")
    rejected = service.decide(
        session, candidate.id, decision="rejected", by="owner", reason="Missing source evidence."
    )
    assert rejected.decision == "rejected"


def test_review_is_for_testing_only_and_never_activates(session: Session, tmp_path) -> None:
    world = source(session)
    candidate = propose(session, world)
    instructions = tmp_path / "prompt.md"
    instructions.write_text(world["snapshot"].system)
    before = instructions.read_bytes()
    control.pause(session, "owner")
    with pytest.raises(control.Paused):
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
    assert service.review_for(session, candidate.id) is None
    control.resume(session, "owner")
    review = service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
    assert (
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner").id
        == review.id
    )
    with pytest.raises(service.TrainingConflict):
        service.decide(session, candidate.id, decision="rejected", by="owner")
    case = service.regression_case(session, candidate)
    assert case is not None and case["private"] is True
    assert case["split"] == "tuning" and case["owner_reviewed"] is False
    assert case["readiness_established"] is False
    assert case["expected"] == EXPECTED and case["system"] == world["snapshot"].system
    assert instructions.read_bytes() == before
    assert world["op"].status == "pending"
    assert session.scalar(select(func.count()).select_from(StagedOperation)) == 1
    assert session.scalar(select(func.count()).select_from(AutonomyLevel)) == 0
    assert session.scalar(select(func.count()).select_from(Suggestion)) == 0


@pytest.mark.parametrize(
    "table", ["training_draft_snapshots", "training_candidates", "training_reviews"]
)
@pytest.mark.parametrize(
    "statement", ["UPDATE {table} SET id=id", "DELETE FROM {table}", "TRUNCATE {table} CASCADE"]
)
def test_training_evidence_is_append_only(session: Session, table: str, statement: str) -> None:
    world = source(session)
    candidate = propose(session, world)
    service.decide(session, candidate.id, decision="rejected", by="owner")
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text(statement.format(table=table)))
    assert session.scalar(text(f"SELECT count(*) FROM {table}")) == 1


@pytest.mark.parametrize("target", ["training_draft_snapshots", "training_candidates"])
def test_tampered_source_or_candidate_cannot_be_reviewed_or_exported(
    session: Session, target: str
) -> None:
    world = source(session)
    candidate = propose(session, world)
    with session.begin_nested():
        session.execute(text(f"ALTER TABLE {target} DISABLE TRIGGER {target}_append_only"))
        column, value = (
            ("system", "tampered") if target == "training_draft_snapshots" else ("rule", "tampered")
        )
        session.execute(text(f"UPDATE {target} SET {column}=:value"), {"value": value})
        session.execute(text(f"ALTER TABLE {target} ENABLE TRIGGER {target}_append_only"))
    session.expire_all()
    with pytest.raises(service.TrainingConflict, match="integrity check failed"):
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
    with pytest.raises(service.TrainingConflict, match="integrity check failed"):
        service.regression_case(session, candidate)
    assert service.review_for(session, candidate.id) is None


def test_concurrent_candidate_retries_create_one_row(fresh_db: str) -> None:
    engine = create_engine(fresh_db)
    db.Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = source(session)
        correction_id, snapshot_id = world["correction"].id, world["snapshot"].id
        session.commit()
    ready = Barrier(2)

    def create():  # type: ignore[no-untyped-def]
        with Session(engine) as session:
            ready.wait(timeout=10)
            candidate = service.create_candidate(
                session,
                correction_id,
                rule="A rule.",
                by="owner",
                snapshot_id=snapshot_id,
                expected_output=EXPECTED,
            )
            candidate_id = candidate.id
            session.commit()
            return candidate_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: create(), range(2)))
        assert ids[0] == ids[1]
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(TrainingCandidate)) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", ["correction_id", "created_by"])
def test_candidate_hash_binds_its_correction_and_owner(session: Session, field: str) -> None:
    world = source(session)
    candidate = propose(session, world)
    original_hash = candidate.payload_sha256
    staged.edit(
        session,
        world["op"].id,
        {"body": "Ask for stronger evidence."},
        by="owner",
        reason="A second source-backed correction.",
        severity="factual",
        version=world["op"].version,
    )
    later = session.scalar(
        select(Correction).where(
            Correction.op_id == world["op"].id, Correction.id != world["correction"].id
        )
    )
    assert later is not None
    # The later correction has the same legitimate workflow/snapshot, so the hash
    # must detect this relink rather than relying on actor/run selection to fail.
    assert world["snapshot"].id in {
        snapshot.id for snapshot in service.eligible_snapshots(session, later)
    }
    value = later.id if field == "correction_id" else "a-different-owner"
    with session.begin_nested():
        session.execute(
            text("ALTER TABLE training_candidates DISABLE TRIGGER training_candidates_append_only")
        )
        session.execute(
            text(f"UPDATE training_candidates SET {field}=:value WHERE id=:id"),
            {"value": value, "id": candidate.id},
        )
        session.execute(
            text("ALTER TABLE training_candidates ENABLE TRIGGER training_candidates_append_only")
        )
    session.expire_all()
    assert candidate.payload_sha256 == original_hash
    with pytest.raises(service.TrainingConflict, match="integrity check failed"):
        service.validate_candidate(session, candidate)
    with pytest.raises(service.TrainingConflict, match="integrity check failed"):
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
    with pytest.raises(service.TrainingConflict, match="integrity check failed"):
        service.regression_case(session, candidate)


@pytest.mark.parametrize("kind", ["snapshot", "candidate"])
@pytest.mark.parametrize("field", ["id", "at"])
def test_integrity_covers_generated_identity_and_time(
    session: Session, kind: str, field: str
) -> None:
    world = source(session)
    original = world["snapshot"] if kind == "snapshot" else propose(session, world)
    model = DraftSnapshot if kind == "snapshot" else TrainingCandidate
    table = model.__tablename__
    old_id, old_hash = original.id, original.payload_sha256
    value = uuid.uuid4() if field == "id" else original.at - timedelta(days=1)
    identifier = value if field == "id" else old_id
    with session.begin_nested():
        session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {table}_append_only"))
        session.execute(
            text(f"UPDATE {table} SET {field}=:value WHERE id=:id"), {"value": value, "id": old_id}
        )
        session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {table}_append_only"))
    session.expire_all()
    changed = session.get(model, identifier)
    assert changed is not None and changed.payload_sha256 == old_hash
    if isinstance(changed, DraftSnapshot):
        with pytest.raises(service.TrainingConflict, match="integrity check failed"):
            service.validate_snapshot(changed)
        with pytest.raises(service.TrainingConflict, match="integrity check failed"):
            service.create_candidate(
                session,
                world["correction"].id,
                rule="A rule.",
                by="owner",
                snapshot_id=changed.id,
                expected_output=EXPECTED,
            )
    else:
        with pytest.raises(service.TrainingConflict, match="integrity check failed"):
            service.validate_candidate(session, changed)
        with pytest.raises(service.TrainingConflict, match="integrity check failed"):
            service.regression_case(session, changed)


def test_persisted_bad_local_reference_is_a_generic_integrity_conflict(session: Session) -> None:
    schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"enum": ["base", "changed"]}},
        "if": {"properties": {"kind": {"const": "changed"}}},
        "then": {"properties": {"detail": {"$ref": "#/$defs/missing"}}, "required": ["detail"]},
    }
    world = source(session, schema=schema, output={"kind": "base"})
    candidate = service.create_candidate(
        session,
        world["correction"].id,
        rule="A rule.",
        by="owner",
        snapshot_id=world["snapshot"].id,
        expected_output={"kind": "base"},
    )
    bad = {"kind": "changed", "detail": "private text"}
    forged_hash = service.digest(
        {
            **service._candidate_payload(candidate),
            **service._record_identity(candidate),
            "expected_output": bad,
        }
    )
    with session.begin_nested():
        session.execute(
            text("ALTER TABLE training_candidates DISABLE TRIGGER training_candidates_append_only")
        )
        session.execute(
            text(
                "UPDATE training_candidates SET expected_output=CAST(:output AS jsonb), "
                "payload_sha256=:hash WHERE id=:id"
            ),
            {"output": json.dumps(bad), "hash": forged_hash, "id": candidate.id},
        )
        session.execute(
            text("ALTER TABLE training_candidates ENABLE TRIGGER training_candidates_append_only")
        )
    session.expire_all()
    with pytest.raises(service.TrainingConflict, match="integrity check failed") as caught:
        service.validate_candidate(session, candidate)
    assert "private text" not in str(caught.value)
