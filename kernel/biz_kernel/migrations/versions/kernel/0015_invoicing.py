"""cap_invoicing_* tables: payment stages per job, invoices (one per milestone), payments.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
MONEY = sa.Numeric(16, 2)


def upgrade() -> None:
    t = "cap_invoicing_stages"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("amount", MONEY, nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("job_id", "name", name="uq_invoicing_stage_name"),
    )
    op.create_index(op.f(f"ix_{t}_job_id"), t, ["job_id"])
    t = "cap_invoicing_invoices"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("party_id", sa.Uuid(), nullable=False),
        sa.Column("milestone_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(120), nullable=True),
        sa.Column("reference", sa.String(120), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
        sa.Column("total", MONEY, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("paid", MONEY, nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("system", sa.String(40), nullable=True),
        sa.Column("external_id", sa.String(120), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("posted_at", TZ, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("milestone_id", name=op.f(f"uq_{t}_milestone_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f(f"uq_{t}_idempotency_key")),
    )
    op.create_index(op.f(f"ix_{t}_job_id"), t, ["job_id"])
    t = "cap_invoicing_payments"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("system", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("paid_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("system", "external_id", name="uq_invoicing_payment_ext"),
    )
    op.create_index(op.f(f"ix_{t}_invoice_id"), t, ["invoice_id"])


def downgrade() -> None:
    op.drop_table("cap_invoicing_payments")
    op.drop_table("cap_invoicing_invoices")
    op.drop_table("cap_invoicing_stages")
