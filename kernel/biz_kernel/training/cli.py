"""Host-only worker for owner-enqueued private local comparisons."""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any

import typer
from dbos import DBOS
from sqlalchemy.exc import SQLAlchemyError

from biz_kernel import db
from biz_kernel.training import comparison
from biz_kernel.training.service import TrainingError
from biz_kernel.workflows.worker import dbos_system_url, ensure_database

app = typer.Typer(help="Run accepted training comparisons on the local appliance host.")


def _launch(url: str | None, system_url: str | None) -> None:
    app_url = url or db.database_url()
    db.configure(app_url)
    # Migrate explicitly with the release procedure first; this worker never mutates the
    # appliance schema or starts the business runtime and its external providers.
    sys_url = system_url or dbos_system_url(app_url)
    ensure_database(sys_url)
    DBOS(
        config={
            "name": "biz-training-comparison",
            "system_database_url": sys_url,
            "application_version": comparison.WORKFLOW_VERSION,
        }
    )  # type: ignore[arg-type]
    DBOS.launch()


def _public_summary(result: dict[str, Any]) -> str:
    # Detailed private inputs, outputs and failures remain in the owner-only API.
    return json.dumps(
        {
            key: result[key]
            for key in (
                "valid",
                "verdict",
                "baseline_passed",
                "candidate_passed",
                "total_cases",
                "known_api_cost_usd",
                "unmeasured_api_cost_results",
                "hardware_cost_usd",
                "readiness_established",
                "activation_permitted",
                "reused",
                "status",
            )
            if key in result
        }
    )


@app.command("run-comparison")
def run_comparison(
    job_id: uuid.UUID,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
    system_url: Annotated[
        str | None, typer.Option("--system-url", envvar="BIZ_DBOS_SYSTEM_URL")
    ] = None,
) -> None:
    """Execute or resume one immutable queued job; never authorize it or activate a rule."""
    try:
        _launch(url, system_url)
        result = comparison.start_comparison(job_id).get_result()
        typer.echo(_public_summary(result))
    except (TrainingError, SQLAlchemyError, ValueError):
        typer.echo("Comparison could not load or persist verified evidence.", err=True)
        raise typer.Exit(1) from None
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=0)
    if result.get("valid") is False:
        raise typer.Exit(1)


@app.command("comparison-worker")
def comparison_worker(
    once: Annotated[bool, typer.Option("--once", help="Drain the current queue and stop")] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", min=0.1, max=60)] = 2,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
    system_url: Annotated[
        str | None, typer.Option("--system-url", envvar="BIZ_DBOS_SYSTEM_URL")
    ] = None,
) -> None:
    """Poll durable jobs from the host; the owner console never performs inference inline."""
    from biz_kernel.training import cohorts

    try:
        _launch(url, system_url)
        while True:
            with db.session_scope() as session:
                pending = cohorts.pending_comparisons(session, limit=10)
            for job_id in pending:
                result = comparison.start_comparison(job_id).get_result()
                typer.echo(f"{job_id} {_public_summary(result)}")
            if once and not pending:
                return
            if not once:
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return
    except (TrainingError, SQLAlchemyError, ValueError):
        typer.echo("Comparison worker could not load or persist verified evidence.", err=True)
        raise typer.Exit(1) from None
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=0)
