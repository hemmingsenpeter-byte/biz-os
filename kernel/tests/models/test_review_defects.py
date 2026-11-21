"""Review regressions: retryable port errors, private case lists, library local tier."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from biz_kernel.models.eval import load_cases, run_eval
from biz_kernel.models.router import Router
from biz_kernel.workflows.runner import _is_permanent
from biz_port_fakes import FakeModel
from test_private_eval import TASK, reviewed  # noqa: F401

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"confidence": {"type": "number"}},
    "required": ["confidence"],
}


class Flaky(FakeModel):
    async def structured(self, system: str, messages: Any, schema: Any, tier: Any) -> Any:
        self.calls.append({"tier": tier})
        raise httpx.ConnectError("cloud unreachable")


def test_transient_cloud_error_during_escalation_stays_retryable() -> None:
    # Local answers with low confidence; the escalated cloud call hits a network blip.
    # Before e7501f0 the ConnectError reached the workflow runner and was retried; now it
    # is wrapped in GenerationFailed (a ValueError) and the SOP step fails permanently.
    router = Router(local=FakeModel([{"confidence": 0.1}]), cloud=Flaky())
    with pytest.raises(Exception) as info:
        asyncio.run(router.generate("t", "s", [], SCHEMA))
    assert not _is_permanent(info.value), type(info.value)


def test_private_cases_passed_as_a_list_still_need_cloud_authorisation(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    _, heldout = reviewed
    cloud = FakeModel()
    with pytest.raises(ValueError):
        asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), load_cases(heldout)))
    assert cloud.calls == []


def test_library_local_tier_refuses_a_remote_ollama_for_private_cases(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    from biz_port_model_ollama import OllamaModel

    _, heldout = reviewed
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            remote = OllamaModel("https://ollama.com", "gpt-oss:120b-cloud", client)
            await run_eval(TASK, "local", Router(remote), heldout)

    with pytest.raises(ValueError):
        asyncio.run(go())
    assert seen == []
