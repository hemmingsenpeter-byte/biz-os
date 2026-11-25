"""Refuse recovery when an unfinished run's SOP definition cannot be replayed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.workflows.models import SopRun

ACTIVE = ("running", "waiting", "budget_stopped", "paused")


class WorkflowCompatibilityError(RuntimeError):
    """Drain the affected work on its original release before starting this one."""


def definition_sha256(spec: Any, sop_name: str) -> str:
    sop = spec.sops[sop_name]
    prompts = {
        step["prompt"]: hashlib.sha256((spec.pack_dir / step["prompt"]).read_bytes()).hexdigest()
        for step in sop["steps"]
        if step["kind"] == "draft"
    }
    bundle = {
        "sop": sop,
        "prompts": prompts,
        "allow_list": sorted(spec.allow_list),
        "escalate_when": [asdict(condition) for condition in spec.escalate_when],
    }
    return hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def unfinished(run: SopRun) -> bool:
    return run.finished_at is None and run.status in ACTIVE


def assert_run_compatible(run: SopRun, roles: dict[str, Any]) -> None:
    if not unfinished(run):
        return
    spec = roles.get(run.role)
    if spec is None or run.sop not in spec.sops:
        problem = "its role or SOP is no longer active"
    elif run.definition_sha256 is None:
        problem = "it has no pinned SOP definition"
    elif run.definition_sha256 != definition_sha256(spec, run.sop):
        problem = "its pinned SOP definition differs from this release"
    else:
        return
    raise WorkflowCompatibilityError(
        f"cannot resume workflow {run.id}: {problem}; "
        "drain it on its original release before upgrading"
    )


def assert_startup_compatible(session: Session, roles: dict[str, Any]) -> None:
    """Call before DBOS launch or accepting production API mutations."""
    for run in session.scalars(
        select(SopRun).where(SopRun.finished_at.is_(None), SopRun.status.in_(ACTIVE))
    ):
        assert_run_compatible(run, roles)
