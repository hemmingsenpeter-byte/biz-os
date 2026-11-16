"""`biz eval <task> --tier local|cloud --cases <dir>`; mount `eval_cmd` or `app` in the main CLI."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from biz_kernel.models.eval.harness import ShadowReport, report_json, run_eval, shadow
from biz_kernel.models.router import Router

app = typer.Typer(help="Score model tasks against golden cases.", no_args_is_help=True)


@app.callback()
def evaluation_commands() -> None:
    """Evaluate a dataset with an explicitly configured backend."""


def build_router() -> Router:
    """Router from the environment (BIZ_OLLAMA_*, ANTHROPIC_API_KEY)."""
    import importlib
    import os

    local = importlib.import_module("biz_port_model_ollama").OllamaModel.create()
    cloud: Any = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        cloud = importlib.import_module("biz_port_model_claude").ClaudeModel.create()
    return Router(local=local, cloud=cloud)


def eval_cmd(
    task: str,
    tier: str = "local",
    cases: Path = Path("."),
    router: Router | None = None,
    *,
    json_output: bool = False,
    output: Path | None = None,
    gate: bool = False,
) -> None:
    """Score complete cases; --gate additionally requires reviewed held-out readiness."""
    if tier not in ("local", "cloud", "shadow"):
        raise typer.BadParameter("tier must be local, cloud or shadow")
    r = router or build_router()
    try:
        rep = (
            asyncio.run(shadow(task, r, cases))
            if tier == "shadow"
            else asyncio.run(run_eval(task, tier, r, cases))
        )  # type: ignore[arg-type]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    serialized = report_json(rep)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(serialized + "\n")
    if json_output:
        typer.echo(serialized)
    elif isinstance(rep, ShadowReport):
        typer.echo(
            f"{task}: local {rep.local.passed_cases}/{rep.local.total_cases} "
            f"({rep.local.pass_rate:.2%}), cloud {rep.cloud.passed_cases}/{rep.cloud.total_cases} "
            f"({rep.cloud.pass_rate:.2%}); comparison_valid={rep.comparison_valid}; "
            f"ready_for_local={rep.ready_for_local}"
        )
    else:
        typer.echo(
            f"{task} [{tier}]: {rep.passed_cases}/{rep.total_cases} passed "
            f"({rep.pass_rate:.2%}); diagnostic check score {rep.check_score:.2%}; "
            f"deployment_ready={rep.deployment_ready}"
        )
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
) -> None:
    eval_cmd(task, tier, cases, json_output=json_output, output=output, gate=gate)
