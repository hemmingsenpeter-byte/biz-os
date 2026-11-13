"""Hash-chain the audit log: `seq`, `prev_hash` and `hash` on `audit_log`, backfilled.

Existing rows are chained in (at, id) order, which is creation order (ids are UUIDv7),
using `biz_kernel.audit.log.entry_hash`, the same encoding `record()` and `verify()` use.

The append-only trigger rejects UPDATE, so the backfill runs like this, all inside the
migration's single transaction:

1. `LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE`, so no other session can read or append
   while the trigger is off and no row can land unchained mid-backfill;
2. `ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only`;
3. fill the three columns in batches;
4. `ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only`.

DDL is transactional in Postgres: if anything fails the transaction rolls back with the
trigger enabled, and no other session ever sees the table with the trigger disabled.

The columns stay nullable so a raw INSERT that bypasses the ORM still succeeds; `verify()`
reports such a row as unchained. `seq` is unique, which also serves the tail lookup.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from biz_kernel.audit.log import GENESIS, entry_hash
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

BATCH = 1000

_audit = sa.table(
    "audit_log",
    sa.column("id", sa.Uuid()),
    sa.column("at", sa.DateTime(timezone=True)),
    sa.column("actor", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("subject", sa.Text()),
    sa.column("detail", postgresql.JSONB()),
    sa.column("seq", sa.BigInteger()),
    sa.column("prev_hash", sa.String()),
    sa.column("hash", sa.String()),
)


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("hash", sa.String(64), nullable=True))

    conn = op.get_bind()
    conn.execute(sa.text("LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE"))
    conn.execute(sa.text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
    _backfill(conn)
    conn.execute(sa.text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))

    op.create_unique_constraint(op.f("uq_audit_log_seq"), "audit_log", ["seq"])


def _backfill(conn: sa.Connection) -> None:
    seq, prev = 0, GENESIS
    last: tuple[object, object] | None = None
    update = (
        sa.update(_audit)
        .where(_audit.c.id == sa.bindparam("b_id"))
        .values(
            seq=sa.bindparam("b_seq"),
            prev_hash=sa.bindparam("b_prev"),
            hash=sa.bindparam("b_hash"),
        )
    )
    cols = [_audit.c[n] for n in ("id", "at", "actor", "kind", "subject", "detail")]
    while True:
        stmt = sa.select(*cols).order_by(_audit.c.at, _audit.c.id).limit(BATCH)
        if last is not None:
            stmt = stmt.where(sa.tuple_(_audit.c.at, _audit.c.id) > sa.tuple_(*last))
        rows = conn.execute(stmt).all()
        if not rows:
            return
        params = []
        for r in rows:
            seq += 1
            digest = entry_hash(
                seq=seq,
                id=r.id,
                at=r.at,
                actor=r.actor,
                kind=r.kind,
                subject=r.subject,
                detail=r.detail,
                prev_hash=prev,
            )
            params.append({"b_id": r.id, "b_seq": seq, "b_prev": prev, "b_hash": digest})
            prev = digest
        conn.execute(update, params)
        last = (rows[-1].at, rows[-1].id)


def downgrade() -> None:
    op.drop_constraint(op.f("uq_audit_log_seq"), "audit_log", type_="unique")
    op.drop_column("audit_log", "hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "seq")
