"""Reply tokens expire.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cap_comms_outbox",
        sa.Column("reply_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "update cap_comms_outbox set reply_expires_at = created_at + interval '14 days' "
        "where reply_token is not null"
    )


def downgrade() -> None:
    op.drop_column("cap_comms_outbox", "reply_expires_at")
