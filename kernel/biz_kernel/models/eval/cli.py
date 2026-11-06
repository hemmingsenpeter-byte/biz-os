"""`biz eval <task> --tier local|cloud --cases <dir>`; mount `eval_cmd` or `app` in the main CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer

from biz_kernel.models.eval.harness import run_eval, shadow
from biz_kernel.models.router import Router

app = typer.Typer(help="Score model tasks against golden cases.", no_args_is_help=True)


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
    task: str, tier: str = "local", cases: Path = Path("."), router: Router | None = None
) -> None:
    """Score `task` on a tier (or both, with tier=shadow); exits 1 if a case fails."""
    r = router or build_router()
    if tier == "shadow":
        rep = asyncio.run(shadow(task, r, cases))
        typer.echo(
            f"{task}: local {rep.local.score:.2f} cloud {rep.cloud.score:.2f} "
            f"agreement {rep.agreement:.2f} ready_for_local={rep.ready_for_local}"
        )
        return
    if tier not in ("local", "cloud"):
        raise typer.BadParameter("tier must be local, cloud or shadow")
    rep1 = asyncio.run(run_eval(task, tier, r, cases))  # type: ignore[arg-type]
    typer.echo(f"{task} [{tier}]: score {rep1.score:.2f}")
    for res in rep1.results:
        typer.echo(f"  {'ok  ' if res.passed else 'FAIL'} {res.name} {'; '.join(res.failures)}")
    if rep1.score < 1.0:
        raise typer.Exit(1)


@app.command("run")
def run_command(
    task: str,
    tier: Annotated[str, typer.Option(help="local, cloud or shadow (both)")] = "local",
    cases: Annotated[Path, typer.Option(help="directory of golden case YAML files")] = Path("."),
) -> None:
    eval_cmd(task, tier, cases)
