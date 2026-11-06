import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from biz_kernel.audit.log import AuditEntry
from biz_kernel.budgets import BudgetBook
from biz_kernel.models import usage
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from sqlalchemy import select

T0 = datetime(2026, 3, 10, tzinfo=UTC)
S = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}


def gen(router, **kw):
    return asyncio.run(router.generate("t", "s", [], S, **kw))


def test_model_tier_default_cloud():
    r = Router(FakeModel(), FakeModel(), task_tiers={"t": "cloud"})
    assert gen(r).tier == "cloud"


def test_local_default_and_high_stakes():
    assert gen(Router(FakeModel(), FakeModel())).tier == "local"
    assert gen(Router(FakeModel(), FakeModel(), high_stakes={"t"})).tier == "cloud"


def test_schema_failure_twice_escalates(session):
    class Bad(FakeModel):
        async def structured(self, system, messages, schema, tier):
            self.calls.append({})
            return {"b": 1}

    local, cloud = Bad(), FakeModel([{"a": "ok"}])
    res = gen(Router(local, cloud), session=session, role="x", now=T0)
    assert (res.tier, len(local.calls), len(cloud.calls)) == ("cloud", 2, 1)
    assert res.escalated
    kinds = [e.kind for e in session.scalars(select(AuditEntry))]
    assert "model.escalated" in kinds


def test_rule_failure_escalates():
    local = FakeModel([{"a": "bad"}, {"a": "bad"}])
    cloud = FakeModel([{"a": "good"}])
    rule = lambda o: "no" if o["a"] == "bad" else None  # noqa: E731
    res = gen(Router(local, cloud), rules=[rule])
    assert res.output == {"a": "good"} and res.escalated


def test_low_confidence_escalates():
    schema = {**S, "properties": {**S["properties"], "confidence": {"type": "number"}}}
    local = FakeModel([{"a": "x", "confidence": 0.1}])
    cloud = FakeModel([{"a": "y", "confidence": 0.9}])
    res = asyncio.run(Router(local, cloud).generate("t", "s", [], schema))
    assert res.tier == "cloud" and len(local.calls) == 1


def test_no_cloud_raises():
    local = FakeModel([{"b": 1}, {"b": 1}])
    local.responses = []  # FakeModel validates, so script via subclass instead

    class Bad(FakeModel):
        async def structured(self, *a):
            return {"b": 1}

    with pytest.raises(ValueError):
        gen(Router(Bad()))


def test_budget_blocked_low_confidence_keeps_local(session):
    schema = {**S, "properties": {**S["properties"], "confidence": {"type": "number"}}}
    book = BudgetBook({"x": 1})
    book.charge(session, "x", 1, "model", None, T0)
    local = FakeModel([{"a": "x", "confidence": 0.1}])
    cloud = FakeModel()
    res = asyncio.run(
        Router(local, cloud, budgets=book).generate(
            "t", "s", [], schema, session=session, role="x", now=T0
        )
    )
    assert res.tier == "local" and res.flagged and not cloud.calls


def test_cost_recorded(session):
    class Priced(FakeModel):
        async def structured(self, *a):
            usage.report(usage.Usage(input_tokens=5, cost_usd=Decimal("0.25")))
            return {"a": "x"}

    book = BudgetBook({"x": 100})
    res = gen(
        Router(FakeModel(), Priced(), task_tiers={"t": "cloud"}, budgets=book),
        session=session,
        role="x",
        now=T0,
    )
    assert res.cost_usd == Decimal("0.25")
    assert book.spent(session, "x", T0) == Decimal("0.25")
