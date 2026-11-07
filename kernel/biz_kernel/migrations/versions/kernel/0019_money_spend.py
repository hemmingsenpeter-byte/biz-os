"""Money spend ledger for daily running totals (`limits.max_daily`).

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "money_spend",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("tool", sa.String(160), nullable=False),
        sa.Column("decision_type", sa.String(120), nullable=True),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_money_spend")),
    )
    op.create_index("ix_money_spend_at", "money_spend", ["at"])


def downgrade() -> None:
    op.drop_index("ix_money_spend_at", table_name="money_spend")
    op.drop_table("money_spend")
