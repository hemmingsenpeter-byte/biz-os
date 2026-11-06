"""Documents fixes: signed copies reference their source version; longer signature status.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cap_documents_renders", sa.Column("source_document_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_cap_documents_renders_source_document_id_documents"),
        "cap_documents_renders",
        "documents",
        ["source_document_id"],
        ["id"],
    )
    op.alter_column(
        "cap_documents_signatures",
        "status",
        existing_type=sa.String(12),
        type_=sa.String(24),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE cap_documents_signatures SET status = 'requested' "
        "WHERE status = 'signed_pending_copy'"
    )
    op.alter_column(
        "cap_documents_signatures",
        "status",
        existing_type=sa.String(24),
        type_=sa.String(12),
        existing_nullable=False,
    )
    op.drop_constraint(
        op.f("fk_cap_documents_renders_source_document_id_documents"),
        "cap_documents_renders",
        type_="foreignkey",
    )
    op.drop_column("cap_documents_renders", "source_document_id")
