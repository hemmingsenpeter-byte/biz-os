"""Every dispatched model attempt retains its reported spend, even if parsing fails."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from biz_kernel.budgets import BudgetBook, CostEvent
from biz_kernel.models import usage
from biz_kernel.models.eval.harness import run_eval
from biz_kernel.models.router import Router, spent_before
from biz_port_fakes import FakeModel
from sqlalchemy import select

T0 = datetime(2026, 11, 27, tzinfo=UTC)
SCHEMA: dict[str, Any] = {"type": "object", "required": ["answer"]}


@pytest.mark.parametrize("prior_attempt", [False, True])
def test_reported_failed_attempt_is_charged_once_and_keeps_exception(session, prior_attempt):
    failure = ConnectionError("provider disconnected after reporting usage")

    class PaidFailure:
        calls = 0

        async def structured(self, system, messages, schema, tier):
            self.calls += 1
            usage.report(usage.Usage(input_tokens=10, cost_usd=Decimal("0.02")))
            if prior_attempt and self.calls == 1:
                return {}  # Validation retries once before the adapter error.
            raise failure

    cloud, book = PaidFailure(), BudgetBook({"worker": 1})
    router = Router(FakeModel(), cloud, budgets=book)
    with pytest.raises(ConnectionError) as info:
        asyncio.run(
            router.generate(
                "task", "system", [], SCHEMA, "cloud", session=session, role="worker", now=T0
            )
        )
    assert info.value is failure
    spent = spent_before(info.value)
    attempts = 2 if prior_attempt else 1
    assert spent is not None and spent.attempts == attempts
    assert spent.tier == "cloud" and spent.unmetered_calls == 0
    assert spent.usage.input_tokens == 10 * attempts
    assert spent.cost_usd == book.spent(session, "worker", T0) == Decimal("0.02") * attempts
    assert len(list(session.scalars(select(CostEvent)))) == attempts


@pytest.mark.parametrize("reported", [None, usage.Usage()])
def test_failed_attempt_distinguishes_unknown_usage_from_reported_zero(reported):
    class Failure:
        async def structured(self, system, messages, schema, tier):
            if reported is not None:
                usage.report(reported)
            raise ConnectionError("failed before output")

    async def evaluate():
        with pytest.raises(ConnectionError) as info:
            await Router(FakeModel(), Failure()).generate("task", "system", [], SCHEMA, "cloud")
        assert usage.take() is None  # A failed attempt cannot contaminate the next one.
        return spent_before(info.value)

    spent = asyncio.run(evaluate())
    assert spent is not None and spent.attempts == 1
    assert spent.unmetered_calls == (1 if reported is None else 0)
    assert spent.cost_usd == 0


def test_failed_unmetered_retry_retains_prior_paid_usage(session):
    class PaidThenUnknown:
        calls = 0

        async def structured(self, system, messages, schema, tier):
            self.calls += 1
            if self.calls == 1:
                usage.report(usage.Usage(cost_usd=Decimal("0.02")))
                return {}
            raise ConnectionError("no usage available for the failed retry")

    book = BudgetBook({"worker": 1})
    with pytest.raises(ConnectionError) as info:
        asyncio.run(
            Router(FakeModel(), PaidThenUnknown(), budgets=book).generate(
                "task", "system", [], SCHEMA, "cloud", session=session, role="worker", now=T0
            )
        )
    spent = spent_before(info.value)
    assert spent is not None and spent.attempts == 2 and spent.unmetered_calls == 1
    assert spent.cost_usd == book.spent(session, "worker", T0) == Decimal("0.02")
    assert len(list(session.scalars(select(CostEvent)))) == 1


def test_cancellation_preserves_reported_usage_and_cancellation_type(session):
    failure = asyncio.CancelledError()

    class CancelledResponse:
        async def structured(self, system, messages, schema, tier):
            usage.report(usage.Usage(cost_usd=Decimal("0.02")))
            raise failure

    book = BudgetBook({"worker": 1})
    with pytest.raises(asyncio.CancelledError) as info:
        asyncio.run(
            Router(FakeModel(), CancelledResponse(), budgets=book).generate(
                "task", "system", [], SCHEMA, "cloud", session=session, role="worker", now=T0
            )
        )
    spent = spent_before(info.value)
    assert info.value is failure and spent is not None
    assert spent.attempts == 1 and spent.cost_usd == book.spent(session, "worker", T0) == Decimal(
        "0.02"
    )
    assert len(list(session.scalars(select(CostEvent)))) == 1


def test_actual_claude_adapter_retains_usage_when_response_has_no_tool_block(session):
    from biz_port_model_claude import ClaudeModel, cost_usd

    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=1), content=[])
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    cloud = ClaudeModel(client=client)  # type: ignore[arg-type]
    book = BudgetBook({"worker": 1})
    with pytest.raises(ValueError, match="no tool_use block") as info:
        asyncio.run(
            Router(FakeModel(), cloud, budgets=book).generate(
                "task", "system", [], SCHEMA, "cloud", session=session, role="worker", now=T0
            )
        )
    spent = spent_before(info.value)
    actual = cost_usd(cloud.model, usage.Usage(input_tokens=10, output_tokens=1))
    assert actual > 0
    assert spent is not None and spent.attempts == 1 and spent.cost_usd == actual
    assert book.spent(session, "worker", T0) == actual
    assert create.await_count == 1


@pytest.mark.parametrize("reported", [None, usage.Usage(), usage.Usage(cost_usd=Decimal("0.02"))])
def test_failed_evaluation_reports_paid_zero_and_unknown_attempts(tmp_path, reported):
    class FailedResponse:
        async def structured(self, system, messages, schema, tier):
            if reported is not None:
                usage.report(reported)
            raise ConnectionError("response could not be decoded")

    (tmp_path / "case.yaml").write_text(yaml.safe_dump({"schema": SCHEMA}))
    report = asyncio.run(run_eval("task", "cloud", Router(FakeModel(), FailedResponse()), tmp_path))
    result = report.results[0]
    assert result.error and not report.deployment_ready
    assert report.metadata["unmetered_cases"] == (1 if reported is None else 0)
    assert Decimal(report.metadata["recorded_model_api_cost_usd"]) == (
        reported.cost_usd if reported is not None else Decimal(0)
    )
    if reported is None:
        assert result.usage is None
    else:
        assert result.usage and result.usage["attempts"] == 1
        assert result.usage["unmetered_calls"] == 0
