"""Append-only training versions, reviewed cohorts and comparison evidence.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TABLES = (
    "training_versions",
    "training_version_reviews",
    "training_cohorts",
    "training_cohort_reviews",
    "training_comparisons",
    "training_comparison_events",
)


def identity() -> tuple[sa.Column, sa.Column]:  # type: ignore[type-arg]
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )


def foreign(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target])


def upgrade() -> None:
    op.create_table(
        "training_versions",
        *identity(),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("parent_sha256", sa.String(64), nullable=True),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("expected_output", postgresql.JSONB(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_versions")),
        foreign("candidate_id", "training_candidates.id"),
        foreign("parent_id", "training_versions.id"),
        foreign("snapshot_id", "training_draft_snapshots.id"),
        sa.UniqueConstraint(
            "candidate_id", "number", name=op.f("uq_training_versions_candidate_id")
        ),
    )
    op.create_table(
        "training_version_reviews",
        *identity(),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("version_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_version_reviews")),
        foreign("version_id", "training_versions.id"),
        sa.UniqueConstraint("version_id", name=op.f("uq_training_version_reviews_version_id")),
    )
    op.create_table(
        "training_cohorts",
        *identity(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("task", sa.String(160), nullable=False),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("members", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_cohorts")),
    )
    op.create_table(
        "training_cohort_reviews",
        *identity(),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_sha256", sa.String(64), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_cohort_reviews")),
        foreign("cohort_id", "training_cohorts.id"),
        sa.UniqueConstraint("cohort_id", name=op.f("uq_training_cohort_reviews_cohort_id")),
    )
    op.create_table(
        "training_comparisons",
        *identity(),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("version_review_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_review_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_comparisons")),
        foreign("version_id", "training_versions.id"),
        foreign("cohort_id", "training_cohorts.id"),
        foreign("version_review_id", "training_version_reviews.id"),
        foreign("cohort_review_id", "training_cohort_reviews.id"),
        sa.UniqueConstraint(
            "created_by", "idempotency_key", name=op.f("uq_training_comparisons_created_by")
        ),
    )
    op.create_table(
        "training_comparison_events",
        *identity(),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("event_key", sa.String(240), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_comparison_events")),
        foreign("job_id", "training_comparisons.id"),
        sa.UniqueConstraint(
            "job_id", "event_key", name=op.f("uq_training_comparison_events_job_id")
        ),
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION biz_reject_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_reject_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION biz_reject_mutation()"
        )


def downgrade() -> None:
    # Comparison evidence has no destructive downgrade path, including under the
    # older allow_data_loss flag. Recover the matching release from a verified backup.
    connection = op.get_bind()
    # Keep the emptiness check and drop atomic with respect to API/worker inserts.
    # The migration advisory lock serializes migrators, but does not lock those writers.
    connection.execute(sa.text(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE"))
    if any(
        connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")) for table in TABLES
    ):
        raise RuntimeError(
            "0025 downgrade would discard comparison evidence; restore a verified backup"
        )
    for table in reversed(TABLES):
        op.drop_table(table)
