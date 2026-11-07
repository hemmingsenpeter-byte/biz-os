"""Autonomy rows that only count approvals carry no level (NULL), so they cannot reset
a level configured in policy.yaml.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("autonomy_levels", "level", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE autonomy_levels SET level = 1 WHERE level IS NULL")
    op.alter_column("autonomy_levels", "level", existing_type=sa.Integer(), nullable=False)
