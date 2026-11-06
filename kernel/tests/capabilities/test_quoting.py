"""Quoting: margin rules, Decimal rounding, versioning, price book import, unpriced lines."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from biz_cap_quoting import config, service
from biz_cap_quoting.models import PriceHistory, PriceItem, Proposal, Scope
from biz_cap_quoting.tools import register
from biz_kernel.tools.registry import ToolRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

JOB = str(uuid.uuid4())
ROWS = [
    {"code": "A1", "category": "base", "description": "Item one", "unit": "m2",
     "unit_cost": "10.00", "unit_price": "15.00"},
    {"code": "B1", "category": "extra", "description": "Item two", "unit": "each",
     "unit_cost": "3.333", "unit_price": "5.555"},
]  # fmt: skip


@pytest.fixture(autouse=True)
def bound() -> Any:
    config.bind(
        SimpleNamespace(
            policy={
                "quoting": {
                    "min_margin_pct": 15,
                    "target_margin_pct": 30,
                    "max_discount_pct": 5,
                    "currency": "EUR",
                }
            }
        )
    )
    yield
    config.bind(None)


@pytest.fixture
def call(session: Session) -> Any:
    reg = ToolRegistry()
    register(reg, {})

    def _call(name: str, **kw: Any) -> Any:
        return reg.get(f"quoting.{name}").fn(session=session, **kw)

    return _call


def _scope(call: Any) -> str:
    return call("scope_write", job_id=JOB, areas=[{"name": "Zone A", "items": []}])["scope_id"]


def test_tool_names_and_side_effects() -> None:
    reg = ToolRegistry()
    register(reg, {})
    names = {n: t.side_effect.value for n, t in reg.tools.items()}
    assert len(names) == 9 and not reg.not_idempotent()
    assert names["quoting.estimate_check"] == "read"
    assert names["quoting.price_book_read"] == "read"
    assert names["quoting.estimate_write"] == "draft"


def test_decimal_rounding_half_up() -> None:
    assert service.money(Decimal("0.005")) == Decimal("0.01")
    assert service.money(Decimal("2.675")) == Decimal("2.68")  # a float would give 2.67
    assert service.dec(0.1) + service.dec(0.2) == Decimal("0.3")
    assert service.minor(Decimal("12.345")) == 1235


def test_margin_policy_ok_below_target_and_below_min(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    sid = _scope(call)
    ok = call("estimate_write", job_id=JOB, scope_id=sid, lines=[{"code": "A1", "quantity": 10}])
    assert ok["total_price"] == "150.00" and ok["total_cost"] == "100.00"
    assert ok["margin_pct"] == 33.33 and ok["margin_ok"] and ok["warnings"] == []
    assert ok["currency"] == "EUR" and ok["total_price_minor"] == 15000
    # price override gives 20% margin: ok but below target
    mid = call(
        "estimate_write",
        job_id=JOB,
        scope_id=sid,
        lines=[{"code": "A1", "quantity": 10, "unit_price": "12.50"}],
    )
    assert mid["margin_ok"] and mid["margin_pct"] == 20.0
    assert any(w.startswith("margin_below_target") for w in mid["warnings"])
    assert any(w.startswith("price_override") for w in mid["warnings"])
    bad = call(
        "estimate_write",
        job_id=JOB,
        scope_id=sid,
        lines=[{"code": "A1", "quantity": 10, "unit_price": "11.00"}],
    )
    assert not bad["margin_ok"]
    assert any(w.startswith("margin_below_policy") for w in bad["warnings"])


def test_line_rounding_and_contingency(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    sid = _scope(call)
    r = call(
        "estimate_write",
        job_id=JOB,
        scope_id=sid,
        lines=[{"code": "B1", "quantity": 3}],
        contingency_pct=10,
    )
    # line cost 9.999 -> 10.00, price 16.665 -> 16.67; contingency x1.10 on rounded totals
    assert r["total_cost"] == "11.00" and r["total_price"] == "18.34"


def test_discount_above_policy_blocks(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    sid = _scope(call)
    r = call(
        "estimate_write",
        job_id=JOB,
        scope_id=sid,
        lines=[{"code": "A1", "quantity": 10}],
        discount_pct=6,
    )
    assert not r["margin_ok"]
    assert any(w.startswith("discount_above_policy") for w in r["warnings"])


def test_unpriced_and_unknown_lines(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    sid = _scope(call)
    with pytest.raises(ValueError, match="unknown_trade_item"):
        call(
            "estimate_write", job_id=JOB, scope_id=sid, lines=[{"description": "x", "quantity": 1}]
        )
    with pytest.raises(ValueError, match="unknown_trade_item"):
        call("estimate_write", job_id=JOB, scope_id=sid, lines=[{"code": "ZZ", "quantity": 1}])
    r = call(
        "estimate_write",
        job_id=JOB,
        scope_id=sid,
        lines=[
            {"code": "A1", "quantity": 10},
            {"description": "Odd item", "quantity": 1, "unit_cost": 50, "unit_price": 80,
             "unpriced": True},
        ],
    )  # fmt: skip
    assert r["unpriced_count"] == 1
    assert any(w.startswith("unpriced_line") for w in r["warnings"])
    chk = call("estimate_check", estimate_id=r["estimate_id"])
    assert any(w.startswith("unpriced_line") for w in chk["warnings"])


def test_cost_basis_stored_per_line(call: Any, session: Session) -> None:
    from biz_cap_quoting.models import EstimateLine

    call("price_book_import", rows=ROWS, source="t")
    r = call(
        "estimate_write", job_id=JOB, scope_id=_scope(call), lines=[{"code": "A1", "quantity": 1}]
    )
    line = session.scalars(
        select(EstimateLine).where(EstimateLine.estimate_id == uuid.UUID(r["estimate_id"]))
    ).one()
    assert line.cost_basis["source"] == "price_book" and line.cost_basis["revision"] == 1


def test_estimate_check_follows_current_policy(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    r = call(
        "estimate_write", job_id=JOB, scope_id=_scope(call), lines=[{"code": "A1", "quantity": 10}]
    )
    assert call("estimate_check", estimate_id=r["estimate_id"])["margin_ok"]
    config.bind(SimpleNamespace(policy={"quoting": {"min_margin_pct": 40}}))
    chk = call("estimate_check", estimate_id=r["estimate_id"])
    assert not chk["margin_ok"] and chk["warnings"]


def test_versioning_supersedes_never_overwrites(call: Any, session: Session) -> None:
    call("price_book_import", rows=ROWS, source="t")
    s1 = call("scope_write", job_id=JOB, areas=[{"name": "A"}])
    s2 = call("scope_write", job_id=JOB, areas=[{"name": "A"}, {"name": "B"}])
    assert (s1["version"], s2["version"]) == (1, 2)
    old = session.get(Scope, uuid.UUID(s1["scope_id"]))
    assert old and old.status == "superseded" and len(old.areas) == 1
    assert str(old.superseded_by) == s2["scope_id"]

    e1 = call(
        "estimate_write", job_id=JOB, scope_id=s2["scope_id"], lines=[{"code": "A1", "quantity": 1}]
    )
    e2 = call(
        "estimate_write", job_id=JOB, scope_id=s2["scope_id"], lines=[{"code": "A1", "quantity": 2}]
    )
    assert (e1["version"], e2["version"]) == (1, 2)
    assert (
        "estimate_superseded: a newer version exists"
        in call("estimate_check", estimate_id=e1["estimate_id"])["warnings"]
    )

    p1 = call("proposal_build", job_id=JOB, estimate_id=e1["estimate_id"])
    p2 = call("proposal_build", job_id=JOB, estimate_id=e2["estimate_id"], valid_days=14)
    assert (p1["version"], p2["version"]) == (1, 2)
    assert session.get(Proposal, uuid.UUID(p1["proposal_id"])).status == "superseded"  # type: ignore[union-attr]
    d = p2["data"]
    assert d["total_price"] == "30.00" and d["currency"] == "EUR" and d["valid_days"] == 14
    assert d["scope"]["areas"] == ["A", "B"] and d["lines"][0]["line_price"] == "30.00"
    assert "unit_cost" not in d["lines"][0]


def test_estimate_requires_current_scope_of_same_job(call: Any) -> None:
    sid = _scope(call)
    with pytest.raises(LookupError):
        call("estimate_write", job_id=str(uuid.uuid4()), scope_id=sid, lines=[])


def test_price_book_import_idempotent_with_history(call: Any, session: Session) -> None:
    assert call("price_book_import", rows=ROWS, source="first") == {
        "imported": 2, "updated": 0, "unchanged": 0,
    }  # fmt: skip
    assert call("price_book_import", rows=ROWS, source="again")["unchanged"] == 2
    assert session.scalars(select(PriceHistory)).all() == []
    changed = [{**ROWS[0], "unit_price": "16.00"}]
    out = call("price_book_import", rows=changed, source="second")
    assert out["updated"] == 1 and out["imported"] == 0
    item = session.get(PriceItem, "A1")
    assert item and item.unit_price == Decimal("16.00") and item.revision == 2
    hist = session.scalars(select(PriceHistory)).all()
    assert len(hist) == 1 and hist[0].unit_price == Decimal("15.00") and hist[0].revision == 1
    assert call("price_book_import", rows=changed, source="third")["unchanged"] == 1
    assert len(session.scalars(select(PriceHistory)).all()) == 1


def test_price_book_read_filters(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    assert [i["code"] for i in call("price_book_read")["items"]] == ["A1", "B1"]
    assert [i["code"] for i in call("price_book_read", categories=["extra"])["items"]] == ["B1"]
    assert [i["code"] for i in call("price_book_read", query="one")["items"]] == ["A1"]


def test_variation_pricing(call: Any) -> None:
    call("price_book_import", rows=ROWS, source="t")
    ok = call(
        "variation_price", job_id=JOB, description="more", lines=[{"code": "A1", "quantity": 4}]
    )
    assert ok["total_price"] == "60.00" and ok["margin_ok"] and ok["total_price_minor"] == 6000
    low = call(
        "variation_price",
        job_id=JOB,
        description="cheap",
        lines=[{"code": "A1", "quantity": 4, "unit_price": "10.50"}],
    )
    assert not low["margin_ok"] and low["margin_pct"] == 4.76
    credit = call(
        "variation_price", job_id=JOB, description="omit", lines=[{"code": "A1", "quantity": -2}]
    )
    assert credit["total_price"] == "-30.00"


def test_brief_survey_roundtrip(call: Any) -> None:
    b = call("brief_write", job_id=JOB, summary="s", requirements=["r"], constraints=["c"])
    s = call(
        "survey_record",
        job_id=JOB,
        checklist_answers={"q": True},
        measurements=[{"area": "Zone A", "quantity": 12.5, "unit": "m2"}],
    )
    assert b["brief_id"] and s["survey_id"]


def test_defaults_when_unbound() -> None:
    config.bind(None)
    p = config.policy()
    assert p.currency == config.DEFAULT_CURRENCY and p.min_margin_pct == Decimal(10)
