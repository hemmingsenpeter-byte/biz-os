"""`biz eval <task> --tier local|cloud|shadow --cases <dir> [--json]`; mount `eval_cmd` or `app`.

Prints passed/total, the pass rate, the promotion gate verdict and each failing case with
why. Exits 0 when the gate passes and 1 when it fails; in shadow mode the local tier's gate
decides, since shadow mode asks whether the local tier can take the task.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer

from biz_kernel.models.eval.harness import report_json, report_text, run_eval, shadow
from biz_kernel.models.router import Router

app = typer.Typer(help="Score model tasks against golden cases.", no_args_is_help=True)

TIERS = ("local", "cloud", "shadow")


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
    as_json: bool = False,
) -> None:
    """Score `task` on a tier (or both, with tier=shadow); exits 1 if the gate fails."""
    if tier not in TIERS:
        raise typer.BadParameter("tier must be local, cloud or shadow")
    r = router or build_router()
    if tier == "shadow":
        srep = asyncio.run(shadow(task, r, cases))
        typer.echo(report_json(srep) if as_json else report_text(srep))
        gate_passed = srep.local.gate.passed
    else:
        rep = asyncio.run(run_eval(task, tier, r, cases))  # type: ignore[arg-type]
        typer.echo(report_json(rep) if as_json else report_text(rep))
        gate_passed = rep.gate.passed
    if not gate_passed:
        raise typer.Exit(1)


@app.command("run")
def run_command(
    task: str,
    tier: Annotated[str, typer.Option(help="local, cloud or shadow (both)")] = "local",
    cases: Annotated[Path, typer.Option(help="directory of golden case YAML files")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="print the report as JSON")] = False,
) -> None:
    eval_cmd(task, tier, cases, as_json=as_json)
