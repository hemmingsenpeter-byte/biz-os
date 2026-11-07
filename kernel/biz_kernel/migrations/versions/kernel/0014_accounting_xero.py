"""port_xero_posts: the Xero adapter's durable ledger (a draft is created once per key).

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "port_xero_posts",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_port_xero_posts")),
    )


def downgrade() -> None:
    op.drop_table("port_xero_posts")
