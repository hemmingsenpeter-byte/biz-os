"""Single-use auth tokens (magic links, invites).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_used_tokens",
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_sha256", name=op.f("pk_auth_used_tokens")),
    )


def downgrade() -> None:
    op.drop_table("auth_used_tokens")
