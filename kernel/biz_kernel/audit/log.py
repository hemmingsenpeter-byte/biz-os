"""Append-only, hash-chained audit log.

A trigger rejects UPDATE and DELETE (see `append_only_ddl`). Because someone with database
access could drop that trigger and edit rows, rows are also hash-chained: each sealed row
stores its position (`seq`, 1, 2, 3, ... with no gaps), the hash of the row before it
(`prev_hash`) and its own `hash` over a canonical encoding of its fields plus `prev_hash`.

Writing takes no lock: `record()` (or any insert) lands a row with the three chain columns
NULL, i.e. unsealed. `seal()` chains every visible unsealed row, ordered by (at, id), in a
short transaction of its own under an advisory lock, so concurrent sealers stay linear and
re-running it is a no-op. `seq` is sealing order, not event order: a row whose transaction
commits late is simply sealed later. The worker seals once per heartbeat and
`biz audit verify` seals before it checks. The audit trigger lets exactly one UPDATE
through: setting all three chain columns on a row whose chain columns are NULL, with every
other column unchanged.

`verify()` walks the chain and reports any row that was changed, removed or re-linked, and
any row left unsealed longer than a grace period.

Limits. Until a row is sealed (at most one heartbeat while the worker runs) someone who drops
the trigger can change or delete it without trace. Rows removed from the end of the chain
leave no trace either: the chain only proves what lies before its head, so detecting
truncation needs `head()` recorded somewhere outside the database (not built).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, Connection, String, Text, event, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from biz_kernel.db import Base, utcnow, uuid7


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(60))  # tool_call | packet | approval | policy | ...
    subject: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Hash chain: NULL until seal() runs.
    seq: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))


# Tables that must never be updated or deleted. Other modules append to this list.
APPEND_ONLY_TABLES: list[str] = ["audit_log"]

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION biz_reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'table %% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

CHAIN_COLUMNS = ("seq", "prev_hash", "hash")

# The audit_log trigger: append-only, except the one update seal() makes. Every column other
# than the chain columns must be unchanged, so a column added later is covered too.
AUDIT_SEAL_FN = """
CREATE OR REPLACE FUNCTION biz_audit_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND OLD.seq IS NULL AND OLD.prev_hash IS NULL AND OLD.hash IS NULL
     AND NEW.seq IS NOT NULL AND NEW.prev_hash IS NOT NULL AND NEW.hash IS NOT NULL
     AND (to_jsonb(NEW) - 'seq' - 'prev_hash' - 'hash')
         IS NOT DISTINCT FROM (to_jsonb(OLD) - 'seq' - 'prev_hash' - 'hash')
  THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION USING MESSAGE = 'table ' || TG_TABLE_NAME || ' is append-only';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_FN = {"audit_log": "biz_audit_append_only"}


def append_only_ddl(table: str) -> str:
    fn = _TRIGGER_FN.get(table, "biz_reject_mutation")
    return (
        f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}();"
    )


@event.listens_for(Base.metadata, "after_create")
def _install_append_only(target: Any, connection: Any, **_: Any) -> None:
    connection.exec_driver_sql(APPEND_ONLY_FN)
    connection.exec_driver_sql(AUDIT_SEAL_FN)
    for table in APPEND_ONLY_TABLES:
        if table in target.tables:
            connection.exec_driver_sql(append_only_ddl(table))


def record(
    session: Session, actor: str, kind: str, subject: str | None = None, **detail: Any
) -> AuditEntry:
    entry = AuditEntry(actor=actor, kind=kind, subject=subject, detail=_jsonable(detail))
    session.add(entry)
    session.flush()
    return entry


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value


# ---- hash chain ------------------------------------------------------------------------

GENESIS = "0" * 64
HASH_VERSION = "biz-audit-v1"
# Transaction-scoped advisory lock key that serialises sealers (any fixed 64-bit value).
SEAL_LOCK_KEY = int.from_bytes(hashlib.sha256(b"biz_audit_log_seal").digest()[:8], "big") >> 1
SEAL_BATCH = 1000
UNSEALED_GRACE = timedelta(minutes=10)


def _canon(value: Any) -> Any:
    # JSONB stores numbers as numeric: 1.0 reads back as 1.0 but 1e20 reads back as an int.
    # Integral floats are encoded as ints so the hash does not depend on that.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canon(v) for v in value]
    return value


def _utc(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def entry_hash(
    *,
    seq: int,
    id: uuid.UUID,
    at: datetime,
    actor: str,
    kind: str,
    subject: str | None,
    detail: Any,
    prev_hash: str,
) -> str:
    """SHA-256 over a canonical JSON encoding of one row's fields and the previous hash."""
    fields = [
        HASH_VERSION,
        seq,
        str(id),
        _utc(at).isoformat(),
        actor,
        kind,
        subject,
        _canon(detail),
        prev_hash,
    ]
    blob = json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_HEAD = text("SELECT seq, hash FROM audit_log WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1")


def head(session: Session) -> tuple[int, str] | None:
    """The last sealed row's (seq, hash), or None for an empty chain."""
    row = session.execute(_HEAD).first()
    return None if row is None else (int(row[0]), str(row[1]))


_UNSEALED = text(
    "SELECT id, at, actor, kind, subject, detail FROM audit_log "
    "WHERE seq IS NULL ORDER BY at, id LIMIT :n"
).columns(detail=JSONB)
_SEAL = text(
    "UPDATE audit_log SET seq = :seq, prev_hash = :prev_hash, hash = :hash "
    "WHERE id = :id AND seq IS NULL"
)


def seal_connection(conn: Connection) -> int:
    """Chain every unsealed row visible to `conn`, inside its current transaction.

    Takes the seal lock first and only then reads the tail, so under READ COMMITTED it sees
    everything the previous sealer committed. The caller commits, which releases the lock.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEAL_LOCK_KEY})
    tail = conn.execute(_HEAD).first()
    seq, prev = (0, GENESIS) if tail is None else (int(tail[0]), str(tail[1]))
    sealed = 0
    while True:
        rows = conn.execute(_UNSEALED, {"n": SEAL_BATCH}).all()
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
            params.append({"id": r.id, "seq": seq, "prev_hash": prev, "hash": digest})
            prev = digest
        if params:
            conn.execute(_SEAL, params)
            sealed += len(params)
        if len(rows) < SEAL_BATCH:
            return sealed


def seal(session: Session) -> int:
    """Seal every visible unsealed row and commit. Returns how many rows were sealed.

    Runs in its own short transaction: give it a session with no other work in progress,
    since it commits. Safe to run concurrently (the lock) and idempotent.
    """
    try:
        sealed = seal_connection(session.connection())
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return sealed


@dataclass(frozen=True)
class Problem:
    """One defect. `kind` is changed | broken_link | deleted | gap | unsealed."""

    kind: str
    seq: int | None
    entry_id: uuid.UUID
    message: str

    def __str__(self) -> str:
        return self.message


_COLUMNS = (
    AuditEntry.seq,
    AuditEntry.id,
    AuditEntry.at,
    AuditEntry.actor,
    AuditEntry.kind,
    AuditEntry.subject,
    AuditEntry.detail,
    AuditEntry.prev_hash,
    AuditEntry.hash,
)


def _rows(session: Session, sealed: bool) -> Iterable[Any]:
    where = AuditEntry.seq.is_not(None) if sealed else AuditEntry.seq.is_(None)
    stmt = select(*_COLUMNS).where(where).order_by(AuditEntry.seq, AuditEntry.at, AuditEntry.id)
    return session.execute(stmt.execution_options(yield_per=1000))


def verify(
    session: Session, grace: timedelta = UNSEALED_GRACE, now: datetime | None = None
) -> list[Problem]:
    """Walk the chain in `seq` order and return every problem, earliest first.

    The first entry is the first broken link. A row whose content no longer matches its
    hash is `changed`; a row whose `prev_hash` is not the previous row's hash is a
    `broken_link`; missing positions are `deleted` (link broken) or a `gap` (the chain was
    re-linked around them). Rows not yet sealed are fine for `grace` after their `at`;
    older ones are `unsealed` (the sealer is not running, or someone is holding rows back).
    Rows removed from the end of the chain leave no trace here: see the module docstring.
    """
    problems: list[Problem] = []
    expected, prev = 1, GENESIS
    for r in _rows(session, sealed=True):
        linked = r.prev_hash == prev
        if r.seq != expected:
            missing = f"{expected}" if r.seq == expected + 1 else f"{expected}..{r.seq - 1}"
            if linked:
                problems.append(
                    Problem("gap", r.seq, r.id, f"seq {r.seq}: positions {missing} are skipped")
                )
            else:
                problems.append(
                    Problem(
                        "deleted", r.seq, r.id, f"seq {r.seq}: rows at seq {missing} were deleted"
                    )
                )
        elif not linked:
            what = "the genesis value" if r.seq == 1 else f"the hash of seq {r.seq - 1}"
            problems.append(
                Problem("broken_link", r.seq, r.id, f"seq {r.seq}: prev_hash does not match {what}")
            )
        recomputed = entry_hash(
            seq=r.seq,
            id=r.id,
            at=r.at,
            actor=r.actor,
            kind=r.kind,
            subject=r.subject,
            detail=r.detail,
            prev_hash=r.prev_hash or "",
        )
        if r.hash != recomputed:
            problems.append(
                Problem("changed", r.seq, r.id, f"seq {r.seq}: row {r.id} does not match its hash")
            )
        expected, prev = r.seq + 1, r.hash
    cutoff = (now or utcnow()) - grace
    for r in _rows(session, sealed=False):
        if r.prev_hash is not None or r.hash is not None:
            problems.append(Problem("changed", None, r.id, f"row {r.id} has a partial seal"))
        elif _utc(r.at) < cutoff:
            problems.append(
                Problem("unsealed", None, r.id, f"row {r.id} from {r.at:%Y-%m-%d %H:%M} unsealed")
            )
    return problems
