"""The audit log is hash-chained: it grows linearly, even under concurrency, and tamper shows."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from biz_kernel.audit import log
from biz_kernel.audit.log import GENESIS, AuditEntry, entry_hash, record, verify
from biz_kernel.db import Base
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


def _chain(eng: Engine) -> list[AuditEntry]:
    with Session(eng) as s:
        return list(s.scalars(select(AuditEntry).order_by(AuditEntry.seq)))


def _verify(eng: Engine) -> list[tuple[str, int | None]]:
    with Session(eng) as s:
        return [(p.kind, p.seq) for p in verify(s)]


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


def test_chain_grows_linearly_and_verifies(eng: Engine) -> None:
    odd = {"f": 1e20, "one": 1.0, "third": 1 / 3, "t": ("a", 2), "u": "Grüße ✓", "none": None}
    first = _append(eng, 3, **odd)
    second = _append(eng, 2)
    rows = _chain(eng)
    assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
    assert [r.id for r in rows] == [e.id for e in first + second]
    assert rows[0].prev_hash == GENESIS
    assert all(b.prev_hash == a.hash for a, b in zip(rows, rows[1:], strict=False))
    assert [r.hash for r in rows] == [e.hash for e in first + second]
    assert _verify(eng) == []
    with Session(eng) as s:
        assert log.head(s) == (5, rows[-1].hash)


def test_entries_added_directly_are_chained_in_add_order(eng: Engine) -> None:
    with Session(eng) as s, s.begin():
        s.add(AuditEntry(actor="a", kind="k", subject="first"))
        s.add(AuditEntry(actor="a", kind="k", subject="second", detail={"x": 1}))
    assert [(r.seq, r.subject) for r in _chain(eng)] == [(1, "first"), (2, "second")]
    assert _verify(eng) == []


def test_rolled_back_append_leaves_no_gap(eng: Engine) -> None:
    _append(eng, 1)
    with Session(eng) as s:
        record(s, "tester", "test")
        s.rollback()
    _append(eng, 1)
    assert [r.seq for r in _chain(eng)] == [1, 2]
    assert _verify(eng) == []


def test_concurrent_appends_from_separate_connections_stay_linear(eng: Engine) -> None:
    workers, txs = 8, 12
    start = threading.Barrier(workers)
    errors: list[BaseException] = []

    def work(w: int) -> None:
        rnd = random.Random(w)
        try:
            start.wait()
            for t in range(txs):
                with Session(eng) as s, s.begin():
                    for i in range(rnd.randint(1, 3)):
                        record(s, f"worker-{w}", "test", f"{t}.{i}")
                        time.sleep(rnd.random() / 500)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert not errors, errors
    rows = _chain(eng)
    assert len({r.actor for r in rows}) == workers
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    assert _verify(eng) == []


def test_changed_row_is_detected(eng: Engine) -> None:
    _append(eng, 3)
    _tamper(eng, "UPDATE audit_log SET detail = '{\"i\": 99}' WHERE seq = 2")
    assert _verify(eng) == [("changed", 2)]


def test_changed_row_with_recomputed_hash_breaks_the_next_link(eng: Engine) -> None:
    rows = _append(eng, 3)
    forged = _rehash(rows[1], actor="someone-else")
    _tamper(eng, "UPDATE audit_log SET actor = 'someone-else', hash = :h WHERE seq = 2", h=forged)
    with Session(eng) as s:
        problems = verify(s)
    assert [(p.kind, p.seq) for p in problems] == [("broken_link", 3)]
    assert "seq 3" in str(problems[0])


def test_deleted_row_is_detected(eng: Engine) -> None:
    _append(eng, 4)
    _tamper(eng, "DELETE FROM audit_log WHERE seq = 2")
    assert _verify(eng) == [("deleted", 3)]


def test_deleted_first_row_is_detected(eng: Engine) -> None:
    _append(eng, 2)
    _tamper(eng, "DELETE FROM audit_log WHERE seq = 1")
    assert _verify(eng) == [("deleted", 2)]


def test_gap_is_detected_even_when_relinked(eng: Engine) -> None:
    rows = _append(eng, 3)
    relinked = _rehash(rows[2], prev_hash=rows[0].hash)
    _tamper(
        eng,
        "DELETE FROM audit_log WHERE seq = 2",
        "UPDATE audit_log SET prev_hash = :p, hash = :h WHERE seq = 3",
        p=rows[0].hash,
        h=relinked,
    )
    assert _verify(eng) == [("gap", 3)]


def test_raw_insert_outside_the_chain_is_reported(eng: Engine) -> None:
    _append(eng, 1)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (id, at, actor, kind, detail) "
                "VALUES (gen_random_uuid(), now(), 'x', 'k', '{}')"
            )
        )
    assert _verify(eng) == [("unchained", None)]


def test_chain_columns_cannot_be_rewritten_while_the_trigger_is_on(eng: Engine) -> None:
    _append(eng, 1)
    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as conn:
        conn.execute(text("UPDATE audit_log SET hash = :h"), {"h": "0" * 64})
