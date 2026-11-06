"""Comms tables: outbox and opt-outs.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cap_comms_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("to", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("expected_to", sa.String(320), nullable=True),
        sa.Column("gate_report", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("sent_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cap_comms_outbox")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_cap_comms_outbox_idempotency_key")),
    )
    op.create_table(
        "cap_comms_optouts",
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("address", name=op.f("pk_cap_comms_optouts")),
    )


def downgrade() -> None:
    op.drop_table("cap_comms_optouts")
    op.drop_table("cap_comms_outbox")
