"""UPDATE, DELETE and TRUNCATE fail on append-only tables, whatever the caller."""

import pytest
from biz_kernel.audit.log import APPEND_ONLY_TABLES, record
from biz_kernel.models_registry import import_all_models
from biz_kernel.packets.bus import Packet
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import_all_models()  # every module appends its tables before we parametrize


def _row(session, table: str):
    if table == "audit_log":
        return record(session, "tester", "test").id
    if table == "packets":
        p = Packet(type="x", schema_version=1, from_part="t", payload={})
        session.add(p)
        session.flush()
        return p.id
    if table == "evidence_events":
        from biz_kernel.evidence.service import note

        return note(session, "test.evidence").id
    if table in ("memory_sources", "memory_writes"):
        from biz_kernel.memory import Cite, HashEmbedder, ingest_source, write_draft

        src = ingest_source(session, "doc", "file://t.md", "Plain fact for the test.")
        if table == "memory_sources":
            return src.id
        write_draft(
            session,
            "t",
            "business",
            "fact",
            "T",
            "Plain fact.",
            [Cite(src.id, "Plain fact", None)],
            HashEmbedder(768),
        )
        return session.execute(text("SELECT id FROM memory_writes LIMIT 1")).scalar()
    if table == "control_pauses":
        from biz_kernel import control
        from biz_kernel.control.models import PauseEvent

        control.pause(session, "tester")
        return session.scalar(select(PauseEvent.id))
    if table == "approval_corrections":
        from biz_kernel.approvals import staged
        from biz_kernel.approvals.models import Correction
        from biz_kernel.policy.types import Action, SideEffect

        action = Action(actor="r", tool="t.send", side_effect=SideEffect.EXTERNAL, inputs={"a": 1})
        op = staged.stage(session, action, "owner")
        staged.edit(session, op.id, {"a": 2}, by="owner", severity="factual", version=op.version)
        return session.scalar(select(Correction.id))
    if table in ("training_draft_snapshots", "training_candidates", "training_reviews"):
        from biz_kernel.approvals.models import Correction
        from biz_kernel.training.models import DraftSnapshot, TrainingCandidate, TrainingReview
        from biz_kernel.workflows.models import SopRun

        correction = session.get(Correction, _row(session, "approval_corrections"))
        run = SopRun(
            id=f"training-invariant:{correction.id}",
            role=correction.actor,
            sop="sample",
            waiting_op_id=str(correction.op_id),
        )
        session.add(run)
        session.flush()
        snapshot = DraftSnapshot(
            run_id=run.id,
            step_id="draft",
            actor=correction.actor,
            task="sample",
            prompt_path="prompts/sample.md",
            system="A fixture prompt.",
            output_schema={"type": "object"},
            untrusted_text="Fixture input.",
            output={"a": 1},
            tier="local",
            backend="fixture",
            system_sha256="0" * 64,
            schema_sha256="0" * 64,
            input_sha256="0" * 64,
            output_sha256="0" * 64,
            payload_sha256="0" * 64,
        )
        session.add(snapshot)
        session.flush()
        if table == "training_draft_snapshots":
            return snapshot.id
        candidate = TrainingCandidate(
            correction_id=correction.id,
            snapshot_id=snapshot.id,
            rule="Use the corrected value.",
            expected_output={"a": 2},
            created_by="owner",
            payload_sha256="0" * 64,
        )
        session.add(candidate)
        session.flush()
        if table == "training_candidates":
            return candidate.id
        review = TrainingReview(candidate_id=candidate.id, decision="rejected", by="owner")
        session.add(review)
        session.flush()
        return review.id
    if table in {
        "training_versions",
        "training_version_reviews",
        "training_cohorts",
        "training_cohort_reviews",
        "training_comparisons",
        "training_comparison_events",
    }:
        from biz_kernel.training import models as training

        # These deliberately minimal rows exercise database mutation guards, not
        # the source/review validation performed by the training service.
        candidate_id = _row(session, "training_candidates")
        version = training.TrainingVersion(
            candidate_id=candidate_id,
            candidate_sha256="0" * 64,
            number=1,
            parent_id=None,
            parent_sha256=None,
            snapshot_id=None,
            source_sha256=None,
            rule="Fixture rule",
            system="Fixture prompt",
            expected_output=None,
            checks=[{"type": "json_schema"}],
            created_by="tester",
            payload_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        if table == "training_versions":
            return version.id
        version_review = training.TrainingVersionReview(
            version_id=version.id,
            version_sha256="0" * 64,
            decision="rejected",
            by="tester",
            reason="Fixture",
            payload_sha256="0" * 64,
        )
        session.add(version_review)
        session.flush()
        if table == "training_version_reviews":
            return version_review.id
        cohort = training.TrainingCohort(
            name="Fixture cohort",
            task="fixture",
            schema_sha256="0" * 64,
            members=[],
            created_by="tester",
            payload_sha256="0" * 64,
        )
        session.add(cohort)
        session.flush()
        if table == "training_cohorts":
            return cohort.id
        cohort_review = training.TrainingCohortReview(
            cohort_id=cohort.id,
            cohort_sha256="0" * 64,
            by="tester",
            reason="Fixture",
            payload_sha256="0" * 64,
        )
        session.add(cohort_review)
        session.flush()
        if table == "training_cohort_reviews":
            return cohort_review.id
        job = training.ComparisonJob(
            version_id=version.id,
            cohort_id=cohort.id,
            version_review_id=version_review.id,
            cohort_review_id=cohort_review.id,
            created_by="tester",
            idempotency_key="fixture",
            contract={},
            request_sha256="0" * 64,
            payload_sha256="0" * 64,
        )
        session.add(job)
        session.flush()
        if table == "training_comparisons":
            return job.id
        event = training.ComparisonEvent(
            job_id=job.id,
            kind="started",
            event_key="fixture",
            payload={},
            payload_sha256="0" * 64,
        )
        session.add(event)
        session.flush()
        return event.id
    return session.execute(text(f"SELECT id FROM {table} LIMIT 1")).scalar()


@pytest.mark.parametrize(
    "table", ["audit_log", "packets", "evidence_events", "memory_sources", "memory_writes"]
)
def test_core_tables_are_registered(table):
    assert table in APPEND_ONLY_TABLES


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE {t} SET id = id WHERE id = :i",
        "DELETE FROM {t} WHERE id = :i",
        "TRUNCATE TABLE {t} CASCADE",
    ],
)
@pytest.mark.parametrize("table", sorted(set(APPEND_ONLY_TABLES)))
def test_audit_append_only(session, table, stmt):
    row_id = _row(session, table)
    assert row_id is not None, f"add a row factory for {table} to _row()"
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text(stmt.format(t=table)), {"i": row_id})
    assert session.execute(text(f"SELECT id FROM {table} WHERE id = :i"), {"i": row_id}).scalar()
