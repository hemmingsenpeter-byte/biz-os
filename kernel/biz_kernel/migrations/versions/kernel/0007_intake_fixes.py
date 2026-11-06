"""Intake identity claims, booking uniqueness.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cap_intake_enquiries", sa.Column("claimed_email", sa.String(320), nullable=True))
    op.add_column("cap_intake_enquiries", sa.Column("claimed_phone", sa.String(320), nullable=True))
    op.add_column(
        "cap_intake_enquiries",
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "cap_booking_meetings",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.create_index(
        "uq_cap_booking_active_start",
        "cap_booking_meetings",
        ["starts_at"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_cap_booking_active_start", table_name="cap_booking_meetings")
    op.drop_column("cap_booking_meetings", "status")
    op.drop_column("cap_intake_enquiries", "verified")
    op.drop_column("cap_intake_enquiries", "claimed_phone")
    op.drop_column("cap_intake_enquiries", "claimed_email")
