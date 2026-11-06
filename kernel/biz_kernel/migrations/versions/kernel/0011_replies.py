"""Reply threading: a token and handler data on the comms outbox.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cap_comms_outbox", sa.Column("reply_token", sa.String(24), nullable=True))
    op.add_column("cap_comms_outbox", sa.Column("meta", JSONB(), nullable=True))
    op.create_index("ix_cap_comms_outbox_reply_token", "cap_comms_outbox", ["reply_token"])


def downgrade() -> None:
    op.drop_index("ix_cap_comms_outbox_reply_token", table_name="cap_comms_outbox")
    op.drop_column("cap_comms_outbox", "meta")
    op.drop_column("cap_comms_outbox", "reply_token")
