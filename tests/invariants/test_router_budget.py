"""Cloud model calls stop at the monthly budget."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from biz_kernel.budgets import BudgetBook, CostEvent
from biz_kernel.models import usage
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from sqlalchemy import func, select

T0 = datetime(2026, 3, 10, tzinfo=UTC)
SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}


class PricedCloud(FakeModel):
    """Each call costs 4 USD."""

    async def structured(self, system, messages, schema, tier):
        out = await super().structured(system, messages, schema, tier)
        usage.report(usage.Usage(cost_usd=Decimal(4)))
        return out


def test_router_budget(session):
    cloud, local = PricedCloud(), FakeModel()
    book = BudgetBook({"clerk": 10})
    router = Router(local, cloud, high_stakes={"big"}, budgets=book)
    results = [
        asyncio.run(router.generate("big", "s", [], SCHEMA, session=session, role="clerk", now=T0))
        for _ in range(5)
    ]
    assert [r.tier for r in results] == ["cloud", "cloud", "local", "local", "local"]
    # 4 + 4 = 8; the next call is estimated at 4 (its last cost) and would overshoot 10
    assert len(cloud.calls) == 2
    assert [r.flagged for r in results] == [False, False, True, True, True]
    assert book.spent(session, "clerk", T0) == 8  # spend stops at or before the limit
    n = session.scalar(select(func.count()).select_from(CostEvent))
    assert n == 2  # paid calls are recorded; nothing more once the next would overshoot


def test_no_cloud_retry_once_budget_is_spent(session):
    """Review finding 3: the budget is checked before every paid call, not once per task."""
    import asyncio
    from datetime import UTC, datetime
    from decimal import Decimal

    import pytest
    from biz_kernel.budgets import BudgetBook
    from biz_kernel.models import usage
    from biz_kernel.models.router import Router
    from biz_port_fakes import FakeModel

    t0 = datetime(2026, 3, 10, tzinfo=UTC)
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}

    class PricedCloud(FakeModel):
        async def structured(self, system, messages, schema, tier):  # type: ignore[no-untyped-def]
            out = await super().structured(system, messages, schema, tier)
            usage.report(usage.Usage(cost_usd=Decimal(4)))
            return out

    cloud = PricedCloud()
    book = BudgetBook({"clerk": 10})
    book.charge(session, "clerk", 8, "model", "seed", t0)
    router = Router(FakeModel(), cloud, high_stakes={"big"}, budgets=book)
    fails = iter([True, False])
    with pytest.raises(ValueError):
        asyncio.run(
            router.generate(
                "big",
                "s",
                [],
                schema,
                session=session,
                role="clerk",
                now=t0,
                rules=[lambda out: "bad" if next(fails) else None],
            )
        )
    assert len(cloud.calls) == 1
    assert book.spent(session, "clerk", t0) == Decimal(12)  # every paid call is recorded


def test_a_refused_cloud_draft_still_counts_against_the_budget(fresh_db):  # type: ignore[no-untyped-def]
    """Paid calls whose output fails validation are real spend: they stay in the ledger,
    so a draft that keeps failing stops at the budget instead of spending without end."""
    from pathlib import Path

    import pytest
    from biz_kernel import db
    from biz_kernel.models_registry import import_all_models
    from biz_kernel.packets.bus import Bus, SchemaRegistry
    from biz_kernel.workflows import runner
    from sqlalchemy import create_engine

    class BadPricedCloud(FakeModel):
        async def structured(self, system, messages, schema, tier):  # type: ignore[no-untyped-def]
            self.calls.append({"tier": tier})
            usage.report(usage.Usage(cost_usd=Decimal(4)))
            return {"wrong": True}

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    try:
        cloud, book = BadPricedCloud(), BudgetBook({"clerk": 10})
        router = Router(FakeModel(), cloud, high_stakes={"big"}, budgets=book)
        runner.install(
            runner.RunnerContext(
                bus=Bus(SchemaRegistry()),
                router=router,
                gate=None,  # type: ignore[arg-type]
                evidence=None,  # type: ignore[arg-type]
                roles={"clerk": runner.RoleSpec("clerk", set(), {}, Path("."))},
                budgets=book,
            )
        )
        with pytest.raises(ValueError, match="failed validation"):
            runner._generate("clerk", "big", "s", SCHEMA, "text")
        now = db.utcnow()
        with db.session_scope() as s:
            assert book.spent(s, "clerk", now) == 8  # both paid tries are on the ledger
        out = runner._generate("clerk", "big", "s", SCHEMA, "text")
        assert out.tier == "local" and out.flagged  # the budget now keeps it off the cloud
        assert len(cloud.calls) == 2
    finally:
        eng.dispose()
