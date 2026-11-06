"""Tables for the intake and booking capabilities.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cap_intake_enquiries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("from", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("classified_as", sa.String(30), nullable=True),
        sa.Column("reason", sa.String(60), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("received_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cap_intake_enquiries")),
        sa.UniqueConstraint("channel", "provider_id", name="uq_intake_provider"),
    )
    op.create_table(
        "cap_intake_optouts",
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("address", name=op.f("pk_cap_intake_optouts")),
    )
    op.create_table(
        "cap_intake_sent",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("to", sa.String(320), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_cap_intake_sent")),
    )
    op.create_table(
        "cap_intake_cursors",
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("value", TZ, nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_cap_intake_cursors")),
    )
    op.create_table(
        "cap_booking_meetings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", TZ, nullable=False),
        sa.Column("ends_at", TZ, nullable=False),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cap_booking_meetings")),
        sa.UniqueConstraint("key", name=op.f("uq_cap_booking_meetings_key")),
    )


def downgrade() -> None:
    for t in (
        "cap_booking_meetings",
        "cap_intake_cursors",
        "cap_intake_sent",
        "cap_intake_optouts",
        "cap_intake_enquiries",
    ):
        op.drop_table(t)
