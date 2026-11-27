"""Immutable draft provenance and owner-reviewed training proposals.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

TABLES = ("training_draft_snapshots", "training_candidates", "training_reviews")


def upgrade() -> None:
    # Refuse before changing the schema, so the old release can still start and drain.
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM sop_runs WHERE finished_at IS NULL "
            "AND status IN ('running', 'waiting', 'paused', 'budget_stopped'))"
        )
    ):
        raise RuntimeError(
            "0024 upgrade requires unfinished workflows to drain on their original release"
        )
    op.add_column("sop_runs", sa.Column("definition_sha256", sa.String(64), nullable=True))
    op.create_table(
        "training_draft_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(200), nullable=False),
        sa.Column("step_id", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("task", sa.String(160), nullable=False),
        sa.Column("prompt_path", sa.Text(), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(), nullable=False),
        sa.Column("untrusted_text", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("tier", sa.String(10), nullable=False),
        sa.Column("backend", sa.String(240), nullable=False),
        sa.Column("configured_model", sa.String(240), nullable=True),
        sa.Column("model_revision", sa.String(240), nullable=True),
        *(
            sa.Column(f"{name}_sha256", sa.String(64), nullable=False)
            for name in ("system", "schema", "input", "output", "payload")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_draft_snapshots")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sop_runs.id"], name=op.f("fk_training_draft_snapshots_run_id_sop_runs")
        ),
        sa.UniqueConstraint("run_id", "step_id", name=op.f("uq_training_draft_snapshots_run_id")),
    )
    op.create_table(
        "training_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correction_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("expected_output", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_candidates")),
        sa.ForeignKeyConstraint(
            ["correction_id"],
            ["approval_corrections.id"],
            name=op.f("fk_training_candidates_correction_id_approval_corrections"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["training_draft_snapshots.id"],
            name=op.f("fk_training_candidates_snapshot_id_training_draft_snapshots"),
        ),
        sa.UniqueConstraint("correction_id", name=op.f("uq_training_candidates_correction_id")),
    )
    op.create_table(
        "training_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_reviews")),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["training_candidates.id"],
            name=op.f("fk_training_reviews_candidate_id_training_candidates"),
        ),
        sa.UniqueConstraint("candidate_id", name=op.f("uq_training_reviews_candidate_id")),
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
    if context.config.attributes.get("allow_data_loss") is not True:
        connection = op.get_bind()
        records = any(
            connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")) for table in TABLES
        )
        pins = connection.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM sop_runs WHERE definition_sha256 IS NOT NULL)")
        )
        if records or pins:
            raise RuntimeError(
                "0024 downgrade drops training records or workflow pins; "
                "restore a backup or explicitly allow_data_loss"
            )
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_column("sop_runs", "definition_sha256")
