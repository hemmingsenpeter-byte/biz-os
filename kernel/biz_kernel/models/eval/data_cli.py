"""Offline data collection and review commands for the model evaluation harness."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from biz_kernel.models.eval.baseline import baseline
from biz_kernel.models.eval.harness import dataset_metadata, load_cases
from biz_kernel.models.eval.private import (
    confirm,
    csv_text,
    prepare,
    private_path,
    spreadsheet_text,
    write_private,
)

app = typer.Typer(help="Prepare owner-reviewed evaluation data and baseline measurements.")


@app.command("prepare")
def prepare_command(
    source: Path,
    workspace: Path,
    version: str,
) -> None:
    """Redact and deduplicate historical enquiries into two private review sheets."""
    try:
        result = prepare(source, workspace, version)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result))


@app.command("confirm")
def confirm_command(
    workspace: Path,
    version: str,
    owner: Annotated[str, typer.Option(help="Owner attesting to every category in this version")],
    template: Annotated[
        Path,
        typer.Option(help="The role's packaged evaluation directory for the task"),
    ],
) -> None:
    """Record explicit owner confirmation and seal the two case sets."""
    try:
        result = confirm(workspace, version, owner, template)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result))


@app.command("review-sheet")
def review_sheet(
    cases: Path,
    output: Path,
) -> None:
    """Export existing synthetic cases for review without editing their provenance."""
    try:
        loaded = load_cases(cases)
        if dataset_metadata(cases, loaded).get("provenance") != "synthetic":
            raise ValueError("review-sheet is for the packaged synthetic cases")
        fields = ("case_id", "message", "expected", "owner_notes")
        rows = [
            {
                "case_id": spreadsheet_text(case.name),
                "message": spreadsheet_text(
                    "\n".join(case.untrusted) or json.dumps(case.messages, ensure_ascii=False)
                ),
                "expected": json.dumps(case.expected, ensure_ascii=False, sort_keys=True),
                "owner_notes": "",
            }
            for case in loaded
        ]
        write_private(private_path(output), csv_text(fields, rows))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Exported {len(loaded)} synthetic cases")


@app.command("baseline")
def baseline_command(
    measurements: Path,
    output: Path,
    week_start: Annotated[datetime, typer.Option(formats=["%Y-%m-%d"])],
    week_end: Annotated[datetime, typer.Option(formats=["%Y-%m-%d"])],
    weekly_enquiries: Annotated[int, typer.Option(min=0)],
) -> None:
    """Validate measurements and write a private baseline report."""
    try:
        report = baseline(measurements, week_start.date(), week_end.date(), weekly_enquiries)
        write_private(private_path(output), json.dumps(report, indent=2) + "\n")
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Summarized {report['owner']['count']} measured owner tasks")
