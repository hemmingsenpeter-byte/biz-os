"""cap_documents_* tables (filled in by the documents capability).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cap_documents_renders",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("template", sa.String(500), nullable=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_cap_documents_renders_document_id_documents"),
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_cap_documents_renders")),
    )
    op.create_index(
        op.f("ix_cap_documents_renders_content_hash"), "cap_documents_renders", ["content_hash"]
    )
    op.create_table(
        "cap_documents_signatures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("system", sa.String(40), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("signers", postgresql.JSONB(), nullable=False),
        sa.Column("signed_by", postgresql.JSONB(), nullable=False),
        sa.Column("signed_document_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_cap_documents_signatures_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["signed_document_id"],
            ["documents.id"],
            name=op.f("fk_cap_documents_signatures_signed_document_id_documents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cap_documents_signatures")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_cap_documents_signatures_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_cap_documents_signatures_document_id"), "cap_documents_signatures", ["document_id"]
    )
    op.create_index(
        op.f("ix_cap_documents_signatures_request_id"), "cap_documents_signatures", ["request_id"]
    )
    op.create_table(
        "port_docuseal_requests",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_port_docuseal_requests")),
    )


def downgrade() -> None:
    op.drop_table("port_docuseal_requests")
    op.drop_table("cap_documents_signatures")
    op.drop_table("cap_documents_renders")
