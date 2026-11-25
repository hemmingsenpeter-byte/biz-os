"""Review regressions, round 2: private cases rebuilt without (or with false) provenance."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from biz_kernel.models.eval import load_cases, run_eval, shadow
from biz_kernel.models.eval.harness import Case, Provenance
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel
from test_private_eval import OUTPUT, TASK, TEMPLATE, reviewed  # noqa: F401


def test_private_cases_rebuilt_without_provenance_still_need_cloud_authorisation(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    _, heldout = reviewed
    stripped = [replace(c, provenance=None) for c in load_cases(heldout)]
    cloud = FakeModel()
    with pytest.raises(ValueError, match="provenance"):
        asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), stripped))
    with pytest.raises(ValueError, match="provenance"):
        asyncio.run(shadow(TASK, Router(FakeModel(), cloud), stripped))
    assert cloud.calls == []


def test_private_cases_copied_field_by_field_still_need_cloud_authorisation(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    _, heldout = reviewed
    rebuilt = [
        Case(c.name, c.system, c.messages, c.schema, c.expected, c.checks, c.untrusted)
        for c in load_cases(heldout)
    ]
    cloud = FakeModel()
    with pytest.raises(ValueError, match="provenance"):
        asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), rebuilt))
    assert cloud.calls == []


def test_private_case_marked_public_in_its_private_directory_counts_as_private(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    _, heldout = reviewed
    relabelled = [
        replace(c, provenance=Provenance("public", str(heldout.resolve())))
        for c in load_cases(heldout)
    ]
    cloud = FakeModel()
    with pytest.raises(ValueError, match="cloud model only with an authorisation"):
        asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), relabelled))
    edited = [replace(relabelled[0], name="not-in-the-dataset")]
    with pytest.raises(ValueError, match="differ from their confirmed dataset"):
        asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), edited))
    assert cloud.calls == []


def test_public_cases_and_local_runs_without_provenance_still_work() -> None:
    public = load_cases(TEMPLATE)
    assert all(c.provenance and c.provenance.kind == "public" for c in public)
    cloud = FakeModel([OUTPUT] * len(public))
    report = asyncio.run(run_eval(TASK, "cloud", Router(FakeModel(), cloud), public))
    assert len(cloud.calls) == len(public)
    assert "cloud_data_disclosure" not in report.metadata
    bare = [replace(c, provenance=None) for c in public[:2]]
    asyncio.run(run_eval(TASK, "local", Router(FakeModel([OUTPUT] * 2)), bare))


def test_harness_rewrites_localhost_ollama_to_the_loopback_address(
    reviewed: tuple[Path, Path],  # noqa: F811
) -> None:
    import httpx
    from biz_port_model_ollama import OllamaModel

    _, heldout = reviewed
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    async def go() -> OllamaModel:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            local = OllamaModel("http://localhost:11434", "llama3", client)
            await run_eval(TASK, "local", Router(local), heldout)
            return local

    original = asyncio.run(go())
    assert seen and all(u.startswith("http://127.0.0.1:11434/") for u in seen)
    assert original.base_url == "http://localhost:11434"  # the caller's port is not mutated
