"""Add statement-level guards to the append-only tables present in revision 0022.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

TABLES = (
    "approval_corrections",
    "audit_log",
    "control_pauses",
    "evidence_events",
    "memory_sources",
    "memory_writes",
    "packets",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_reject_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION biz_reject_mutation()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_reject_truncate ON {table}")
