"""The audit log is hash-chained by a sealer: writes take no lock, the chain stays linear
under concurrent writers and sealers, and tampering shows."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from biz_kernel.audit import log
from biz_kernel.audit.log import GENESIS, AuditEntry, entry_hash, record, seal, verify
from biz_kernel.db import Base, utcnow
from biz_kernel.models_registry import import_all_models
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


@pytest.fixture
def eng(fresh_db: str) -> Iterator[Engine]:
    """A committed database of its own, so verify() sees exactly the rows a test wrote."""
    import_all_models()
    engine = create_engine(fresh_db)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _append(eng: Engine, n: int, **detail: Any) -> list[AuditEntry]:
    with Session(eng, expire_on_commit=False) as s, s.begin():
        return [record(s, "tester", "test", f"row {i}", i=i, **detail) for i in range(n)]


def _seal(eng: Engine) -> int:
    with Session(eng) as s:
        return seal(s)


def _chain(eng: Engine) -> list[AuditEntry]:
    with Session(eng) as s:
        return list(s.scalars(select(AuditEntry).order_by(AuditEntry.seq, AuditEntry.at)))


def _verify(eng: Engine, **kw: Any) -> list[tuple[str, int | None]]:
    with Session(eng) as s:
        return [(p.kind, p.seq) for p in verify(s, **kw)]


def _tamper(eng: Engine, *statements: str, **params: Any) -> None:
    """What someone with database access could do: drop the trigger, edit, put it back."""
    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_log_append_only ON audit_log"))
        for sql in statements:
            conn.execute(text(sql), params)
        conn.exec_driver_sql(log.append_only_ddl("audit_log"))


def _rehash(entry: AuditEntry, **changes: Any) -> str:
    fields: dict[str, Any] = {
        "seq": entry.seq,
        "id": entry.id,
        "at": entry.at,
        "actor": entry.actor,
        "kind": entry.kind,
        "subject": entry.subject,
        "detail": entry.detail,
        "prev_hash": entry.prev_hash,
    }
    return entry_hash(**(fields | changes))


def _sealed(eng: Engine, n: int) -> list[AuditEntry]:
    _append(eng, n)
    assert _seal(eng) == n
    return _chain(eng)


# ---------------------------------------------------------------- chain


def test_record_writes_unsealed_rows_and_seal_chains_them(eng: Engine) -> None:
    odd = {"f": 1e20, "one": 1.0, "third": 1 / 3, "t": ("a", 2), "u": "Grüße ✓", "none": None}
    first = _append(eng, 3, **odd)
    assert all(e.seq is None and e.prev_hash is None and e.hash is None for e in first)
    assert _seal(eng) == 3
    second = _append(eng, 2)
    assert _seal(eng) == 2
    assert _seal(eng) == 0  # idempotent
    rows = _chain(eng)
    assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
    assert [r.id for r in rows] == [e.id for e in first + second]
    assert rows[0].prev_hash == GENESIS
    assert all(b.prev_hash == a.hash for a, b in zip(rows, rows[1:], strict=False))
    assert _verify(eng) == []
    with Session(eng) as s:
        assert log.head(s) == (5, rows[-1].hash)


def test_seal_orders_by_at_then_id_and_late_commits_get_later_seqs(eng: Engine) -> None:
    now = utcnow()
    with Session(eng) as s, s.begin():
        s.add(AuditEntry(actor="a", kind="k", subject="later", at=now))
        s.add(AuditEntry(actor="a", kind="k", subject="earlier", at=now - timedelta(seconds=5)))
    _seal(eng)
    with Session(eng) as s, s.begin():  # commits late, with an older `at`
        s.add(AuditEntry(actor="a", kind="k", subject="late", at=now - timedelta(hours=1)))
    _seal(eng)
    assert [(r.seq, r.subject) for r in _chain(eng)] == [(1, "earlier"), (2, "later"), (3, "late")]
    assert _verify(eng) == []


def test_writing_takes_no_lock_while_a_sealer_holds_it(eng: Engine) -> None:
    _append(eng, 1)
    with Session(eng) as sealer:
        log.seal_connection(sealer.connection())  # holds the seal lock, uncommitted
        with Session(eng) as writer, writer.begin():
            writer.execute(text("SET LOCAL lock_timeout = '2s'"))
            record(writer, "tester", "test")  # must not wait for the sealer
        sealer.commit()
    assert _seal(eng) == 1
    assert _verify(eng) == []


def test_concurrent_writers_and_sealers_stay_linear(eng: Engine) -> None:
    writers, sealers, txs = 6, 3, 12
    start = threading.Barrier(writers + sealers)
    done = threading.Event()
    errors: list[BaseException] = []

    def write(w: int) -> None:
        rnd = random.Random(w)
        try:
            start.wait()
            for t in range(txs):
                with Session(eng) as s, s.begin():
                    for i in range(rnd.randint(1, 3)):
                        record(s, f"writer-{w}", "test", f"{t}.{i}")
                        time.sleep(rnd.random() / 500)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def keep_sealing() -> None:
        try:
            start.wait()
            while not done.is_set():
                _seal(eng)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    ws = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    ss = [threading.Thread(target=keep_sealing) for _ in range(sealers)]
    for th in ws + ss:
        th.start()
    for th in ws:
        th.join(60)
    done.set()
    for th in ss:
        th.join(60)
    _seal(eng)
    assert not errors, errors
    rows = _chain(eng)
    assert len({r.actor for r in rows}) == writers
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    assert _verify(eng) == []


# ---------------------------------------------------------------- trigger


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE audit_log SET actor = 'x' WHERE seq IS NULL",
        "UPDATE audit_log SET id = id WHERE seq IS NULL",
        "UPDATE audit_log SET seq = 99, prev_hash = :h, hash = :h, actor = 'x' WHERE seq IS NULL",
        "UPDATE audit_log SET seq = 99, prev_hash = :h, hash = :h, detail = '{}' WHERE seq IS NULL",
        "UPDATE audit_log SET seq = 99, prev_hash = :h, hash = :h, at = now() WHERE seq IS NULL",
        "UPDATE audit_log SET seq = 99 WHERE seq IS NULL",
        "UPDATE audit_log SET seq = 99, hash = :h WHERE seq IS NULL",
        "UPDATE audit_log SET seq = seq + 100 WHERE seq IS NOT NULL",
        "UPDATE audit_log SET hash = :h WHERE seq IS NOT NULL",
        "UPDATE audit_log SET seq = NULL, prev_hash = NULL, hash = NULL WHERE seq IS NOT NULL",
        "UPDATE audit_log SET actor = 'x' WHERE seq IS NOT NULL",
        "DELETE FROM audit_log WHERE seq IS NULL",
        "DELETE FROM audit_log WHERE seq IS NOT NULL",
    ],
)
def test_trigger_rejects_every_update_but_the_seal(eng: Engine, stmt: str) -> None:
    _sealed(eng, 1)
    _append(eng, 1)  # one sealed row, one unsealed row
    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(text(stmt), {"h": "f" * 64})


def test_trigger_allows_exactly_the_seal(eng: Engine) -> None:
    _append(eng, 1)
    with eng.begin() as conn:
        n = conn.execute(
            text("UPDATE audit_log SET seq = 1, prev_hash = :h, hash = :h WHERE seq IS NULL"),
            {"h": "f" * 64},
        ).rowcount
    assert n == 1


# ---------------------------------------------------------------- tamper


def test_changed_row_is_detected(eng: Engine) -> None:
    _sealed(eng, 3)
    _tamper(eng, "UPDATE audit_log SET detail = '{\"i\": 99}' WHERE seq = 2")
    assert _verify(eng) == [("changed", 2)]


def test_changed_row_with_recomputed_hash_breaks_the_next_link(eng: Engine) -> None:
    rows = _sealed(eng, 3)
    forged = _rehash(rows[1], actor="someone-else")
    _tamper(eng, "UPDATE audit_log SET actor = 'someone-else', hash = :h WHERE seq = 2", h=forged)
    with Session(eng) as s:
        problems = verify(s)
    assert [(p.kind, p.seq) for p in problems] == [("broken_link", 3)]
    assert "seq 3" in str(problems[0])


def test_deleted_row_is_detected(eng: Engine) -> None:
    _sealed(eng, 4)
    _tamper(eng, "DELETE FROM audit_log WHERE seq = 2")
    assert _verify(eng) == [("deleted", 3)]


def test_deleted_first_row_is_detected(eng: Engine) -> None:
    _sealed(eng, 2)
    _tamper(eng, "DELETE FROM audit_log WHERE seq = 1")
    assert _verify(eng) == [("deleted", 2)]


def test_gap_is_detected_even_when_relinked(eng: Engine) -> None:
    rows = _sealed(eng, 3)
    relinked = _rehash(rows[2], prev_hash=rows[0].hash)
    _tamper(
        eng,
        "DELETE FROM audit_log WHERE seq = 2",
        "UPDATE audit_log SET prev_hash = :p, hash = :h WHERE seq = 3",
        p=rows[0].hash,
        h=relinked,
    )
    assert _verify(eng) == [("gap", 3)]


def test_rows_left_unsealed_past_the_grace_period_are_reported(eng: Engine) -> None:
    _sealed(eng, 1)
    _append(eng, 1)
    assert _verify(eng) == []  # fresh: within the grace period
    later = utcnow() + timedelta(minutes=11)
    assert _verify(eng, now=later) == [("unsealed", None)]
    assert _verify(eng, now=later, grace=timedelta(hours=1)) == []
    _seal(eng)
    assert _verify(eng, now=later) == []


# ---------------------------------------------------------------- worker


def test_worker_heartbeat_seals_what_the_tick_recorded(
    eng: Engine, fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biz_kernel import db
    from biz_kernel.workflows.worker import run_once

    monkeypatch.setattr(db, "_engine", db._engine)
    monkeypatch.setattr(db, "_factory", db._factory)
    configured = db.configure(fresh_db)

    class Runtime:
        def tick(self, now: Any) -> list[str]:
            with db.session_scope() as s:
                record(s, "role", "heartbeat")
            return ["role"]

    try:
        assert run_once(Runtime()) == ["role"]
    finally:
        configured.dispose()
    assert [(r.seq, r.kind) for r in _chain(eng)] == [(1, "heartbeat")]
    assert _verify(eng) == []
