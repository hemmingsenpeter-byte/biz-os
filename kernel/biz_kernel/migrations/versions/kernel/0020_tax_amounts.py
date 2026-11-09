"""Tax on invoices and bills: stored amounts stay tax-exclusive, the tax sits beside them.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TABLES = ("cap_invoicing_invoices", "cap_payables_bills")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table, sa.Column("tax", sa.Numeric(16, 2), nullable=False, server_default="0")
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "tax")
