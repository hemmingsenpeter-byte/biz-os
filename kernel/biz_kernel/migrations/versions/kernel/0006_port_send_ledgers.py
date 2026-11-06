"""Port send ledgers (durable idempotency for outbound sends).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = ("port_twilio_sends", "port_m365_sends")


def upgrade() -> None:
    for name in TABLES:
        op.create_table(
            name,
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("status", sa.String(12), nullable=False),
            sa.Column("provider_id", sa.String(200), nullable=True),
            sa.Column("result", postgresql.JSONB(), nullable=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key", name=op.f(f"pk_{name}")),
        )


def downgrade() -> None:
    for name in reversed(TABLES):
        op.drop_table(name)
