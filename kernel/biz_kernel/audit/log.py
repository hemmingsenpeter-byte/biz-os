"""Append-only, hash-chained audit log.

A trigger rejects UPDATE and DELETE (see `append_only_ddl`). Because someone with database
access could drop that trigger and edit rows, every row is also chained: it stores its
position (`seq`, 1, 2, 3, ... with no gaps), the hash of the row before it (`prev_hash`) and
its own `hash` over a canonical encoding of its fields plus `prev_hash`. `verify()` walks
the chain and reports any row that was changed, removed or re-linked.

Chaining happens at flush time for every new `AuditEntry`, whoever added it. To keep the
chain linear when the API and the worker append concurrently, the flush takes a
transaction-scoped advisory lock and only then reads the tail, so appends are serialised
from the first audit row a transaction writes until that transaction ends. The unique
index on `seq` is the backstop: a writer that somehow reads a stale tail fails instead of
forking the chain.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, String, Text, event, inspect, select, text
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
    # Hash chain. Nullable only so a raw INSERT that bypasses the ORM still lands; verify()
    # reports such a row as unchained.
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


def append_only_ddl(table: str) -> str:
    return (
        f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION biz_reject_mutation();"
    )


@event.listens_for(Base.metadata, "after_create")
def _install_append_only(target: Any, connection: Any, **_: Any) -> None:
    connection.exec_driver_sql(APPEND_ONLY_FN)
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
# Transaction-scoped advisory lock key that serialises appends (any fixed 64-bit value).
CHAIN_LOCK_KEY = int.from_bytes(hashlib.sha256(b"biz_audit_log_chain").digest()[:8], "big") >> 1


def _canon(value: Any) -> Any:
    # JSONB stores numbers as numeric: 1.0 reads back as 1.0 but 1e20 reads back as an int.
    # Integral floats are encoded as ints so the hash is the same before and after the trip.
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


def head(session: Session) -> tuple[int, str] | None:
    """The last chained row's (seq, hash), or None for an empty chain."""
    row = session.execute(
        text("SELECT seq, hash FROM audit_log WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1")
    ).first()
    return None if row is None else (int(row[0]), str(row[1]))


def _new_entries(session: Session) -> list[AuditEntry]:
    entries = [obj for obj in session.new if isinstance(obj, AuditEntry) and obj.seq is None]
    # Chain in the order the entries were added to the session.
    return sorted(entries, key=lambda e: inspect(e).insert_order or 0)


@event.listens_for(Session, "before_flush")
def _chain_new_entries(session: Session, _ctx: Any, _instances: Any) -> None:
    entries = _new_entries(session)
    if not entries:
        return
    # Held until this transaction commits or rolls back. The tail is read in a later
    # statement, so under READ COMMITTED it sees every append committed by the last holder.
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK_KEY})
    seq, prev = head(session) or (0, GENESIS)
    for entry in entries:
        seq += 1
        if entry.id is None:
            entry.id = uuid7()
        entry.at = _utc(entry.at if entry.at is not None else utcnow())
        # Store exactly what JSONB will hold, so the hash matches the row when read back.
        entry.detail = json.loads(json.dumps(entry.detail if entry.detail is not None else {}))
        entry.seq, entry.prev_hash = seq, prev
        entry.hash = prev = entry_hash(
            seq=seq,
            id=entry.id,
            at=entry.at,
            actor=entry.actor,
            kind=entry.kind,
            subject=entry.subject,
            detail=entry.detail,
            prev_hash=entry.prev_hash,
        )


@dataclass(frozen=True)
class Problem:
    """One defect in the chain. `kind` is changed | broken_link | deleted | gap | unchained."""

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


def _rows(session: Session, chained: bool) -> Iterable[Any]:
    where = AuditEntry.seq.is_not(None) if chained else AuditEntry.seq.is_(None)
    stmt = select(*_COLUMNS).where(where).order_by(AuditEntry.seq, AuditEntry.id)
    return session.execute(stmt.execution_options(yield_per=1000))


def verify(session: Session) -> list[Problem]:
    """Walk the chain in `seq` order and return every problem, earliest first.

    The first entry is the first broken link. A row whose content no longer matches its
    hash is `changed`; a row whose `prev_hash` is not the previous row's hash is a
    `broken_link`; missing positions are `deleted` (link broken) or a `gap` (the chain was
    re-linked around them); a row with no chain fields is `unchained`. Rows removed from the
    end of the chain leave no trace here: compare `head()` with a copy kept elsewhere.
    """
    problems: list[Problem] = []
    expected, prev = 1, GENESIS
    for r in _rows(session, chained=True):
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
    for r in _rows(session, chained=False):
        problems.append(Problem("unchained", None, r.id, f"row {r.id} is not in the chain"))
    return problems
