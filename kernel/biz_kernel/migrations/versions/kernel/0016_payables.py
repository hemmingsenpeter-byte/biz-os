"""cap_payables_* tables: PO copies, bills, verified bank accounts, bank change requests.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
MONEY = sa.Numeric(16, 2)


def upgrade() -> None:
    t = "cap_payables_pos"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_party_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(80), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("total", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("supplier_party_id", "number", name="uq_payables_po_num"),
    )
    op.create_index(op.f(f"ix_{t}_supplier_party_id"), t, ["supplier_party_id"])
    t = "cap_payables_bills"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_party_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(80), nullable=False),
        sa.Column("po_number", sa.String(80), nullable=True),
        sa.Column("po_id", sa.Uuid(), nullable=True),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
        sa.Column("total", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("due", TZ, nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("problems", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("system", sa.String(40), nullable=True),
        sa.Column("external_id", sa.String(120), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("supplier_party_id", "number", name="uq_payables_bill_num"),
        sa.UniqueConstraint("idempotency_key", name=op.f(f"uq_{t}_idempotency_key")),
    )
    op.create_index(op.f(f"ix_{t}_supplier_party_id"), t, ["supplier_party_id"])
    t = "cap_payables_bank_accounts"
    op.create_table(
        t,
        sa.Column("supplier_party_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.String(64), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=False),
        sa.Column("verified_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("supplier_party_id", name=op.f(f"pk_{t}")),
    )
    t = "cap_payables_bank_changes"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_party_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.String(64), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("callback_phone", sa.String(40), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("verified_by", sa.String(120), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("applied_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_supplier_party_id"), t, ["supplier_party_id"])


def downgrade() -> None:
    op.drop_table("cap_payables_bank_changes")
    op.drop_table("cap_payables_bank_accounts")
    op.drop_table("cap_payables_bills")
    op.drop_table("cap_payables_pos")
