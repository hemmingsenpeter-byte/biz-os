"""0025 preserves populated 0024 evidence and never destroys comparison records."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from biz_kernel import migrations
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import Correction
from biz_kernel.db import utcnow
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.training import service
from biz_kernel.workflows.models import SopRun, SopStep
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

NEW_TABLES = (
    "training_versions",
    "training_version_reviews",
    "training_cohorts",
    "training_cohort_reviews",
    "training_comparisons",
    "training_comparison_events",
)
HISTORY = (
    "sop_runs",
    "sop_steps",
    "staged_operations",
    "approval_corrections",
    "training_draft_snapshots",
    "training_candidates",
    "training_reviews",
    "audit_log",
)


def history(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            table: hashlib.sha256(
                json.dumps(
                    list(
                        connection.scalars(
                            text(
                                f"SELECT to_jsonb(row) FROM {table} AS row "
                                "ORDER BY to_jsonb(row)::text"
                            )
                        )
                    ),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for table in HISTORY
        }


def seed_0024(engine: Engine) -> None:
    """Use the existing proposal APIs against only the original 0024 schema."""
    with Session(engine) as session:
        op = staged.stage(
            session,
            Action(
                actor="fixture",
                tool="fixture.send",
                side_effect=SideEffect.EXTERNAL,
                inputs={"body": "Original answer"},
                decision_type="reply",
            ),
            "owner",
        )
        run = SopRun(
            id="migration-evidence",
            role="fixture",
            sop="draft",
            status="waiting",
            waiting_op_id=str(op.id),
            definition_sha256="0" * 64,
        )
        session.add(run)
        session.flush()
        snapshot = service.capture_draft(
            session,
            run_id=run.id,
            step_id="draft",
            actor="fixture",
            task="fixture.draft",
            prompt_path="prompts/draft.md",
            system="Prepare a factual answer.",
            schema={
                "type": "object",
                "required": ["body"],
                "properties": {"body": {"type": "string"}},
            },
            untrusted_text="Synthetic request",
            output={"body": "Original answer"},
            tier="local",
            backend="fixture",
            configured_model=None,
        )
        expected = {"body": "Corrected answer"}
        staged.edit(
            session,
            op.id,
            expected,
            by="owner",
            reason="A factual correction.",
            severity="factual",
            version=op.version,
        )
        correction = session.scalar(select(Correction).where(Correction.op_id == op.id))
        assert correction is not None
        candidate = service.create_candidate(
            session,
            correction.id,
            rule="Check evidence before replying.",
            by="owner",
            snapshot_id=snapshot.id,
            expected_output=expected,
        )
        service.decide(session, candidate.id, decision="accepted_for_testing", by="owner")
        session.add(SopStep(run_id=run.id, step_id="send:staged", output={"op_id": str(op.id)}))
        staged.reject(session, op.id, by="owner", reason="Use the corrected answer manually.")
        run.status, run.finished_at, run.waiting_op_id = "rejected", utcnow(), None
        session.commit()


def test_populated_0024_history_survives_upgrade_and_empty_comparison_downgrade(
    fresh_db: str,
) -> None:
    migrations.upgrade(fresh_db, "0024")
    engine = create_engine(fresh_db)
    try:
        seed_0024(engine)
        before = history(engine)
        migrations.upgrade(fresh_db)
        migrations.upgrade(fresh_db)
        assert history(engine) == before
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
            for table in NEW_TABLES:
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        migrations.downgrade(fresh_db, "0024")
        assert history(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("allow_data_loss", [False, True])
def test_populated_comparison_downgrade_is_always_refused(
    fresh_db: str, allow_data_loss: bool
) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO training_cohorts (id, at, name, task, schema_sha256, members, "
                    "created_by, payload_sha256) VALUES "
                    "(gen_random_uuid(), now(), 'Migration fixture', 'fixture.draft', :hash, "
                    "'[]', 'owner', :hash)"
                ),
                {"hash": "0" * 64},
            )
        with pytest.raises(RuntimeError, match="discard comparison evidence"):
            migrations.downgrade(fresh_db, "0024", allow_data_loss=allow_data_loss)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
            assert connection.scalar(text("SELECT count(*) FROM training_cohorts")) == 1
    finally:
        engine.dispose()


def test_downgrade_waits_for_inflight_insert_then_refuses_to_discard_it(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    try:
        with engine.connect() as writer:
            transaction = writer.begin()
            writer.execute(
                text(
                    "INSERT INTO training_cohorts (id, at, name, task, schema_sha256, members, "
                    "created_by, payload_sha256) VALUES "
                    "(gen_random_uuid(), now(), 'Concurrent fixture', 'fixture.draft', :hash, "
                    "'[]', 'owner', :hash)"
                ),
                {"hash": "0" * 64},
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(migrations.downgrade, fresh_db, "0024")
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        with engine.connect() as observer:
                            blocked = observer.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                    "WHERE datname=current_database() AND wait_event_type='Lock' "
                                    "AND query LIKE 'LOCK TABLE training_versions%')"
                                )
                            )
                        if blocked:
                            break
                        if future.done():
                            pytest.fail(
                                "downgrade passed its emptiness check without locking writers"
                            )
                        time.sleep(0.02)
                    else:
                        pytest.fail("downgrade did not wait for the in-flight evidence insert")
                finally:
                    transaction.commit()
                with pytest.raises(RuntimeError, match="discard comparison evidence"):
                    future.result(timeout=10)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
            assert connection.scalar(text("SELECT count(*) FROM training_cohorts")) == 1
    finally:
        engine.dispose()
