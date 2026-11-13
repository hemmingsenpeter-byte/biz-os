"""Overdue reminders: one per invoice per poll, the latest step due, none while paused."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from biz_kernel import control, db
from biz_kernel.models_registry import import_all_models
from sqlalchemy import create_engine


class _Bus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, session: Any, **packet: Any) -> None:
        self.published.append(packet)


class _Rt:
    def __init__(self) -> None:
        self.bus = _Bus()


@pytest.fixture
def live(fresh_db: str, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import biz_cap_invoicing.models  # noqa: F401

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    monkeypatch.setattr(db, "_engine", db._engine)
    monkeypatch.setattr(db, "_factory", db._factory)
    configured = db.configure(fresh_db)
    yield
    configured.dispose()
    eng.dispose()


def _invoice(days_overdue: int, reminders_sent: int = 0) -> uuid.UUID:
    from biz_cap_invoicing.models import Invoice

    with db.session_scope() as s:
        inv = Invoice(
            job_id=uuid.uuid4(),
            party_id=uuid.uuid4(),
            reference="INV-1",
            currency="NZD",
            total=Decimal("100"),
            status="posted",
            reminders_sent=reminders_sent,
            due=db.utcnow() - timedelta(days=days_overdue, hours=1),
        )
        s.add(inv)
        s.flush()
        return inv.id


def _sent(inv_id: uuid.UUID) -> int:
    from biz_cap_invoicing.models import Invoice

    with db.session_scope() as s:
        return s.get(Invoice, inv_id).reminders_sent


def test_a_late_poll_sends_only_the_latest_reminder(live) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_invoicing.overdue import poll_overdue

    inv = _invoice(days_overdue=31)  # steps 7, 14 and 30 all came due unseen
    rt = _Rt()
    assert poll_overdue(rt) == 1
    (packet,) = rt.bus.published
    assert packet["payload"]["reminder"] == 3
    assert packet["idempotency_key"].endswith(":3")
    assert _sent(inv) == 3
    assert poll_overdue(rt) == 0  # nothing left


def test_one_step_at_a_time_when_polled_on_schedule(live) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_invoicing.overdue import poll_overdue

    inv = _invoice(days_overdue=8)
    rt = _Rt()
    assert poll_overdue(rt) == 1 and rt.bus.published[0]["payload"]["reminder"] == 1
    assert poll_overdue(rt) == 0 and _sent(inv) == 1


def test_no_reminders_while_paused(live) -> None:  # type: ignore[no-untyped-def]
    from biz_cap_invoicing.overdue import poll_overdue

    inv = _invoice(days_overdue=15)
    with db.session_scope() as s:
        control.pause(s, "owner", "checking")
    rt = _Rt()
    assert poll_overdue(rt) == 0 and rt.bus.published == [] and _sent(inv) == 0
    with db.session_scope() as s:
        control.resume(s, "owner")
    assert poll_overdue(rt) == 1 and rt.bus.published[0]["payload"]["reminder"] == 2
