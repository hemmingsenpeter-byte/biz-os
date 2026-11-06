"""cap_voice_capture_* tables (filled in by the voice_capture capability).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cap_voice_capture_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("audio_uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("language", sa.String(20), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cap_voice_capture_notes")),
        sa.UniqueConstraint("sha256", name=op.f("uq_cap_voice_capture_notes_sha256")),
    )


def downgrade() -> None:
    op.drop_table("cap_voice_capture_notes")
