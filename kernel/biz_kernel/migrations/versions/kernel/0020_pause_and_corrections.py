"""Kill switch events (`control_pauses`) and approver corrections (`approval_corrections`).

Both are append-only.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APPEND_ONLY = ["control_pauses", "approval_corrections"]


def upgrade() -> None:
    op.create_table(
        "control_pauses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_pauses")),
    )
    op.create_table(
        "approval_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("op_id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("tool", sa.String(160), nullable=False),
        sa.Column("decision_type", sa.String(120), nullable=True),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(10), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_corrections")),
    )
    op.create_index(
        op.f("ix_approval_corrections_op_id"), "approval_corrections", ["op_id"], unique=False
    )
    for table in APPEND_ONLY:
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION biz_reject_mutation()"
            )
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_approval_corrections_op_id"), table_name="approval_corrections")
    op.drop_table("approval_corrections")
    op.drop_table("control_pauses")
