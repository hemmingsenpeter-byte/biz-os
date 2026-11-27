"""Training history upgrades match the ORM and survive a refused downgrade."""

from __future__ import annotations

import pytest
from biz_kernel import migrations
from biz_kernel.db import Base
from biz_kernel.models_registry import import_all_models
from sqlalchemy import UniqueConstraint, create_engine, inspect, text

TABLES = (
    "training_draft_snapshots",
    "training_candidates",
    "training_reviews",
    "training_versions",
    "training_version_reviews",
    "training_cohorts",
    "training_cohort_reviews",
    "training_comparisons",
    "training_comparison_events",
)


def test_legacy_work_must_drain_before_any_upgrade_changes(fresh_db: str) -> None:
    migrations.upgrade(fresh_db, "0023")
    engine = create_engine(fresh_db)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sop_runs (id, role, sop, status, started_at) "
                    "VALUES ('legacy-pending', 'fixture', 'sample', 'waiting', now())"
                )
            )
        with pytest.raises(RuntimeError, match="drain on their original release"):
            migrations.upgrade(fresh_db)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0023"
            assert (
                connection.scalar(text("SELECT status FROM sop_runs WHERE id='legacy-pending'"))
                == "waiting"
            )
        assert "definition_sha256" not in {
            column["name"] for column in inspect(engine).get_columns("sop_runs")
        }
        assert set(TABLES).isdisjoint(inspect(engine).get_table_names())
        # Settling on the old schema is still possible; then the new release can upgrade.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sop_runs SET status='done', finished_at=now() WHERE id='legacy-pending'"
                )
            )
        migrations.upgrade(fresh_db)
        assert set(TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_training_columns_and_constraints_match_metadata(fresh_db: str) -> None:
    import_all_models()
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    try:
        inspector = inspect(engine)
        for name in TABLES:
            metadata = Base.metadata.tables[name]
            assert {
                column["name"]: (
                    column["type"].compile(dialect=engine.dialect),
                    column["nullable"],
                )
                for column in inspector.get_columns(name)
            } == {
                column.name: (column.type.compile(dialect=engine.dialect), column.nullable)
                for column in metadata.columns
            }
            assert {
                tuple(item["column_names"]) for item in inspector.get_unique_constraints(name)
            } == {
                tuple(column.name for column in constraint.columns)
                for constraint in metadata.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert {
                (
                    tuple(item["constrained_columns"]),
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                )
                for item in inspector.get_foreign_keys(name)
            } == {
                (
                    tuple(column.name for column in constraint.columns),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                )
                for constraint in metadata.foreign_key_constraints
            }
    finally:
        engine.dispose()


def test_populated_training_downgrade_requires_explicit_data_loss(fresh_db: str) -> None:
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sop_runs (id, role, sop, status, started_at) "
                    "VALUES ('training-migration', 'fixture', 'sample', 'done', now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO training_draft_snapshots "
                    "(id, at, run_id, step_id, actor, task, prompt_path, system, output_schema, "
                    "untrusted_text, output, tier, backend, system_sha256, schema_sha256, "
                    "input_sha256, output_sha256, payload_sha256) VALUES "
                    "(gen_random_uuid(), now(), 'training-migration', 'draft', "
                    "'fixture', 'sample', "
                    "'prompts/sample.md', 'Fixture prompt', '{}', 'Fixture source', '{}', 'local', "
                    "'fixture', :hash, :hash, :hash, :hash, :hash)"
                ),
                {"hash": "0" * 64},
            )
        with pytest.raises(RuntimeError, match="drops training records"):
            migrations.downgrade(fresh_db, "0023")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
            assert connection.scalar(text("SELECT count(*) FROM training_draft_snapshots")) == 1
        migrations.downgrade(fresh_db, "0023", allow_data_loss=True)
        assert set(TABLES).isdisjoint(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM sop_runs")) == 1
    finally:
        engine.dispose()
