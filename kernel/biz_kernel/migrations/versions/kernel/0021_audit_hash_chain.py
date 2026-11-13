"""Hash-chain the audit log: `seq`, `prev_hash` and `hash` on `audit_log`, sealed.

The old append-only trigger on `audit_log` rejected every UPDATE. It is replaced by
`biz_audit_append_only()`, which still rejects every UPDATE and DELETE except the one the
sealer makes: setting all three chain columns on a row whose chain columns are NULL, with
every other column unchanged. With that trigger in place the backfill is just a seal
(`biz_kernel.audit.log.seal_connection`): existing rows are chained in (at, id) order,
inside this migration's transaction, without ever disabling the trigger.

The columns stay nullable: a row is unsealed between its insert and the next seal.
`seq` is unique, which also serves the tail lookup.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from biz_kernel.audit.log import AUDIT_SEAL_FN, seal_connection

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _trigger(fn: str) -> str:
    return (
        "DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;"
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
    )


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("hash", sa.String(64), nullable=True))
    op.create_unique_constraint(op.f("uq_audit_log_seq"), "audit_log", ["seq"])
    op.execute(sa.text(AUDIT_SEAL_FN))
    op.execute(sa.text(_trigger("biz_audit_append_only")))
    seal_connection(op.get_bind())


def downgrade() -> None:
    op.execute(sa.text(_trigger("biz_reject_mutation")))
    op.execute(sa.text("DROP FUNCTION IF EXISTS biz_audit_append_only()"))
    op.drop_constraint(op.f("uq_audit_log_seq"), "audit_log", type_="unique")
    op.drop_column("audit_log", "hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "seq")
