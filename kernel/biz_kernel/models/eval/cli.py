"""`biz eval <task> --tier local|cloud --cases <dir>`; mount `eval_cmd` or `app` in the main CLI."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

import httpx
import typer

from biz_kernel.models.eval.harness import (
    EvalReport,
    ShadowReport,
    is_private,
    read_task,
    report_json,
    run_eval,
    shadow,
)
from biz_kernel.models.eval.private import private_path, write_private
from biz_kernel.models.router import Router, Tier

app = typer.Typer(help="Score model tasks against golden cases.", no_args_is_help=True)


@app.callback()
def evaluation_commands() -> None:
    """Evaluate a dataset with an explicitly configured backend."""


def build_router() -> Router:
    """Router from the environment (BIZ_OLLAMA_*, ANTHROPIC_API_KEY)."""
    import importlib

    local = importlib.import_module("biz_port_model_ollama").OllamaModel.create()
    cloud: Any = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        cloud = importlib.import_module("biz_port_model_claude").ClaudeModel.create()
    return Router(local=local, cloud=cloud)


def _loopback_ollama_url(url: str, model: str) -> str:
    problem = "private local evaluation requires a loopback Ollama URL and a noncloud model"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError(problem)
    except ValueError as exc:
        raise ValueError(problem) from exc
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.search(r"(?:^|[^a-z0-9])cloud(?:$|[^a-z0-9])", model.lower())
    ):
        raise ValueError(problem)
    if host == "localhost":
        # Use the actual loopback address rather than system name resolution.
        parsed = parsed._replace(netloc=f"127.0.0.1:{port}" if port else "127.0.0.1")
    return parsed.geturl()


async def _score(
    task: str,
    tier: Literal["local", "cloud", "shadow"],
    router: Router,
    cases: Path,
    versions: dict[Tier, str],
    *,
    private_local: bool,
) -> EvalReport | ShadowReport:
    async with AsyncExitStack() as stack:
        if private_local:
            from biz_port_model_ollama import OllamaModel

            port = router.local
            if isinstance(port, OllamaModel):
                url = _loopback_ollama_url(port.base_url, port.model)
                client = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=port.timeout, trust_env=False)
                )
                local = OllamaModel(
                    url,
                    port.model,
                    client,
                    embed_model=port.embed_model,
                    temperature=port.temperature,
                    max_concurrency=port.max_concurrency,
                    timeout=port.timeout,
                )
                router = replace(router, local=local)
        if tier == "shadow":
            return await shadow(task, router, cases, model_versions=versions)
        return await run_eval(task, tier, router, cases, model_version=versions.get(tier))


def eval_cmd(
    task: str,
    tier: str = "local",
    cases: Path = Path("."),
    router: Router | None = None,
    *,
    json_output: bool = False,
    output: Path | None = None,
    gate: bool = False,
    allow_cloud_data: bool = False,
    local_model_version: str = "",
    cloud_model_version: str = "",
) -> None:
    """Score complete cases; --gate additionally requires reviewed held-out readiness."""
    if tier not in ("local", "cloud", "shadow"):
        raise typer.BadParameter("tier must be local, cloud or shadow")
    evaluation_tier = cast(Literal["local", "cloud", "shadow"], tier)
    versions: dict[Tier, str] = {}
    if local_model_version.strip():
        versions["local"] = local_model_version.strip()
    if cloud_model_version.strip():
        versions["cloud"] = cloud_model_version.strip()
    try:
        private = is_private(read_task(cases))
        private_local = private and tier != "cloud"
        if private:
            cases = private_path(cases)
            if output is None or json_output:
                raise ValueError("private evaluations require --output and omit --json")
            output = private_path(output)
            if output.is_relative_to(cases):
                raise ValueError("write the evaluation report outside the case directory")
            if tier in ("cloud", "shadow") and not allow_cloud_data:
                raise ValueError("private cloud evaluation requires --allow-cloud-data")
            required = {"local", "cloud"} if tier == "shadow" else {tier}
            if not required.issubset(versions):
                raise ValueError("supply the explicit model version for each requested tier")
        if private_local and router is None:
            from biz_port_model_ollama import DEFAULT_MODEL, DEFAULT_URL

            _loopback_ollama_url(
                os.environ.get("BIZ_OLLAMA_URL", DEFAULT_URL),
                os.environ.get("BIZ_OLLAMA_MODEL", DEFAULT_MODEL),
            )
        r = router or build_router()
        rep = asyncio.run(
            _score(task, evaluation_tier, r, cases, versions, private_local=private_local)
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    serialized = report_json(rep)
    if output is not None:
        write_private(output, serialized + "\n")
    if json_output:
        typer.echo(serialized)
    elif isinstance(rep, ShadowReport):
        typer.echo(
            f"{task}: local {rep.local.passed_cases}/{rep.local.total_cases} "
            f"({rep.local.pass_rate:.2%}), cloud {rep.cloud.passed_cases}/{rep.cloud.total_cases} "
            f"({rep.cloud.pass_rate:.2%}); comparison_valid={rep.comparison_valid}; "
            f"technical_gate_passed={rep.technical_gate_passed}; "
            f"ready_for_local={rep.ready_for_local}"
        )
    else:
        typer.echo(
            f"{task} [{tier}]: {rep.passed_cases}/{rep.total_cases} passed "
            f"({rep.pass_rate:.2%}); diagnostic check score {rep.check_score:.2%}; "
            f"technical_gate_passed={rep.technical_gate_passed}; "
            f"deployment_ready={rep.deployment_ready}"
        )
        if not private:
            for result in rep.results:
                typer.echo(
                    f"  {'ok  ' if result.passed else 'FAIL'} {result.name} "
                    f"{'; '.join(result.failures)}"
                )
    if isinstance(rep, ShadowReport):
        passed = rep.technical_gate_passed and (not gate or rep.ready_for_local)
    else:
        quality = (
            rep.technical_gate_passed
            if rep.metadata.get("split") == "heldout"
            else rep.pass_rate == 1.0
        )
        passed = quality and (not gate or rep.deployment_ready)
    if not passed:
        raise typer.Exit(1)


@app.command("run")
def run_command(
    task: str,
    tier: Annotated[str, typer.Option(help="local, cloud or shadow (both)")] = "local",
    cases: Annotated[Path, typer.Option(help="directory of golden case YAML files")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    gate: Annotated[bool, typer.Option("--gate")] = False,
    allow_cloud_data: Annotated[bool, typer.Option("--allow-cloud-data")] = False,
    local_model_version: str = "",
    cloud_model_version: str = "",
) -> None:
    eval_cmd(
        task,
        tier,
        cases,
        json_output=json_output,
        output=output,
        gate=gate,
        allow_cloud_data=allow_cloud_data,
        local_model_version=local_model_version,
        cloud_model_version=cloud_model_version,
    )
