"""The measurement report: what each role's decision types have earned, and what it cost.

Read-only: nothing here writes, flushes or commits. A number that cannot be measured yet
is None ("not measured"), never 0.

Where each number comes from, and where it is weaker than its definition:

- proposals and their buckets: `staged_operations` created in the window, joined to
  `approval_corrections`. Versioned edits without correction history are reported as unknown.
- level: the *stored* level in `autonomy_levels` (owner-set, promoted or demoted). None
  means nothing is stored and the configured default applies; the effective level also
  depends on the tool and policy.yaml, which only the policy engine resolves.
- recorded_demotions: audit entries written by `autonomy.record_outcome` for a bad outcome
  (kind "autonomy", how "demote") in the window. The lifetime `bad_outcomes` counter is
  not used because it has no dates. Actual serious_errors is unmeasured until
  production reversal/outcome capture exists.
- tasks_completed: tasks have no completion time of their own. A task counts when it is
  done and a workflow run that finished "done" in the window is linked to it; a task
  completed any other way is not counted.
- cost_usd: `cost_events` in the window.
- time_saved_minutes: needs the owner's timed baseline from the `metrics:` section of
  policy.yaml; without a usable one it is None.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biz_kernel.approvals.models import AutonomyLevel, Correction, StagedOperation
from biz_kernel.audit.log import AuditEntry
from biz_kernel.budgets.models import CostEvent
from biz_kernel.db import utcnow
from biz_kernel.records.models import Task
from biz_kernel.workflows.models import SopRun

COSMETIC = "cosmetic"
APPROVED_STATUSES = ("approved", "executed")
DEFAULT_DAYS, MAX_DAYS = 28, 366
NOT_MEASURED = "not measured"


class DecisionTypeRow(BaseModel):
    role: str
    decision_type: str
    level: int | None
    proposals: int
    approved_unchanged: int
    approved_cosmetic: int
    approved_with_errors: int
    approved_unknown: int
    rejected: int
    pending: int
    approval_without_edit_rate: float | None
    error_free_rate: float | None


class MetricsReport(BaseModel):
    since: datetime
    until: datetime
    decision_types: list[DecisionTypeRow]
    serious_errors: int | None
    recorded_demotions: int = 0
    tasks_completed: int
    cost_usd: float
    cost_per_task_usd: float | None
    time_saved_minutes: float | None


def window(days: int = DEFAULT_DAYS, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The last `days` days up to now, as (since, until)."""
    until = now or utcnow()
    return until - timedelta(days=days), until


def _minutes(value: Any) -> float | None:
    """A finite, non-negative number of minutes, else None. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def config_problems(config: Any) -> list[str]:
    """Problems in the `metrics:` section of policy.yaml (biz doctor).

    No section, or one without `baseline_minutes`, is fine: time saved is then reported
    as not measured. At runtime any problem listed here also means not measured.
    """
    if config is None:
        return []
    if not isinstance(config, dict):
        return ["metrics must be a mapping"]
    baseline = config.get("baseline_minutes")
    if baseline is None:
        return []
    if not isinstance(baseline, dict) or not baseline:
        return ["metrics.baseline_minutes must map task kinds (or default) to minutes"]
    out = [
        f"metrics.baseline_minutes.{kind}: {value!r} is not a number of minutes"
        for kind, value in baseline.items()
        if _minutes(value) is None
    ]
    if _minutes(config.get("review_minutes_per_approval")) is None:
        out.append(
            "metrics.review_minutes_per_approval must be a number of minutes "
            "when baseline_minutes is set"
        )
    return out


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _decision_types(session: Session, since: datetime, until: datetime) -> list[DecisionTypeRow]:
    in_window = (StagedOperation.created_at >= since, StagedOperation.created_at < until)
    ops = session.execute(
        select(
            StagedOperation.id,
            StagedOperation.actor,
            StagedOperation.decision_type,
            StagedOperation.status,
            StagedOperation.version,
        ).where(*in_window)
    ).all()
    corrections: dict[uuid.UUID, list[tuple[int, int, str | None]]] = defaultdict(list)
    for op_id, start, end, severity in session.execute(
        select(
            Correction.op_id, Correction.from_version, Correction.to_version, Correction.severity
        ).where(Correction.op_id.in_(select(StagedOperation.id).where(*in_window)))
    ):
        corrections[op_id].append((start, end, severity))
    stored = {
        (role, decision_type): level
        for role, decision_type, level in session.execute(
            select(AutonomyLevel.role, AutonomyLevel.decision_type, AutonomyLevel.level)
        )
    }
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for op_id, actor, decision_type, status, version in ops:
        c = counts[(actor, decision_type or "")]
        c["proposals"] += 1
        if status in APPROVED_STATUSES:
            edits = corrections.get(op_id, [])
            ordered = sorted(edits, key=lambda edit: edit[:2])
            complete = len(edits) == version - 1 and all(
                (start, end) == (n, n + 1) for n, (start, end, _) in enumerate(ordered, start=1)
            )
            if any(severity != COSMETIC for _, _, severity in edits):
                # A known error remains an error even when older edits are missing.
                c["approved_with_errors"] += 1
            elif not complete:
                c["approved_unknown"] += 1
            elif edits:
                c["approved_cosmetic"] += 1
            else:
                c["approved_unchanged"] += 1
        elif status == "rejected":
            c["rejected"] += 1
        else:
            c["pending"] += 1
    rows = []
    for (role, decision_type), c in sorted(counts.items()):
        decided = (
            c["approved_unchanged"]
            + c["approved_cosmetic"]
            + c["approved_with_errors"]
            + c["approved_unknown"]
        )
        rows.append(
            DecisionTypeRow(
                role=role,
                decision_type=decision_type,
                level=stored.get((role, decision_type)),
                proposals=c["proposals"],
                approved_unchanged=c["approved_unchanged"],
                approved_cosmetic=c["approved_cosmetic"],
                approved_with_errors=c["approved_with_errors"],
                approved_unknown=c["approved_unknown"],
                rejected=c["rejected"],
                pending=c["pending"],
                approval_without_edit_rate=_rate(c["approved_unchanged"], c["proposals"]),
                error_free_rate=(
                    None
                    if c["approved_unknown"]
                    else _rate(c["approved_unchanged"] + c["approved_cosmetic"], decided)
                ),
            )
        )
    return rows


def _completed_task_kinds(session: Session, since: datetime, until: datetime) -> list[str]:
    """Kind of each task completed in the window (one entry per task)."""
    done_in_window = select(SopRun.task_id).where(
        SopRun.status == "done",
        SopRun.task_id.is_not(None),
        SopRun.finished_at >= since,
        SopRun.finished_at < until,
    )
    return list(
        session.scalars(select(Task.kind).where(Task.status == "done", Task.id.in_(done_in_window)))
    )


def _time_saved(kinds: list[str], reviews: int, config: Any) -> float | None:
    if not isinstance(config, dict) or not config.get("baseline_minutes"):
        return None
    if config_problems(config):
        return None
    baseline: dict[str, Any] = config["baseline_minutes"]
    total = 0.0
    for kind in kinds:
        minutes = baseline.get(kind, baseline.get("default"))
        if minutes is None:  # a completed task nobody timed: the total would be a guess
            return None
        total += float(minutes)
    return total - reviews * float(config["review_minutes_per_approval"])


def report(
    session: Session, since: datetime, until: datetime, config: dict[str, Any] | None = None
) -> MetricsReport:
    """The measurement report for `since <= t < until`.

    `config` is the `metrics:` section of the composition's policy.yaml, if any.

    Approval without edit is unchanged approvals divided by all proposals. Error-free
    rate describes approved requests only and is unknown when legacy edits lack
    correction provenance. Rejections and pending requests remain visible.

    Time saved is the timed baseline for each completed task, minus the review minutes
    for every proposal in the window that the approver decided (approved or rejected).
    It can be negative. It is None when no usable baseline is configured, or when a
    completed task's kind has neither its own baseline nor a default.
    """
    rows = _decision_types(session, since, until)
    serious = session.scalar(
        select(func.count())
        .select_from(AuditEntry)
        .where(
            AuditEntry.kind == "autonomy",
            AuditEntry.detail["how"].astext == "demote",
            AuditEntry.at >= since,
            AuditEntry.at < until,
        )
    )
    kinds = _completed_task_kinds(session, since, until)
    cost = session.scalar(
        select(func.sum(CostEvent.amount_usd)).where(CostEvent.at >= since, CostEvent.at < until)
    ) or Decimal(0)
    reviews = sum(
        r.approved_unchanged
        + r.approved_cosmetic
        + r.approved_with_errors
        + r.approved_unknown
        + r.rejected
        for r in rows
    )
    return MetricsReport(
        since=since,
        until=until,
        decision_types=rows,
        serious_errors=None,
        recorded_demotions=int(serious or 0),
        tasks_completed=len(kinds),
        cost_usd=round(float(cost), 6),
        cost_per_task_usd=round(float(cost) / len(kinds), 6) if kinds else None,
        time_saved_minutes=_time_saved(kinds, reviews, config),
    )


def _pct(rate: float | None) -> str:
    return NOT_MEASURED if rate is None else f"{rate:.0%}"


def render(rep: MetricsReport) -> str:
    """A plain-text table for the owner."""
    lines = [
        f"From {rep.since:%d %b %Y} to {rep.until:%d %b %Y}",
        "",
        f"Tasks completed:  {rep.tasks_completed}",
        f"Serious errors:   {NOT_MEASURED if rep.serious_errors is None else rep.serious_errors}",
        f"Recorded demotions: {rep.recorded_demotions}",
        f"Cost:             ${rep.cost_usd:.2f}",
        "Cost per task:    "
        + (NOT_MEASURED if rep.cost_per_task_usd is None else f"${rep.cost_per_task_usd:.2f}"),
        "Time saved:       "
        + (
            f"{NOT_MEASURED} (no timed baseline in policy.yaml)"
            if rep.time_saved_minutes is None
            else f"{rep.time_saved_minutes / 60:.1f} hours"
        ),
        "",
    ]
    if not rep.decision_types:
        return "\n".join([*lines, "No proposals in this period."])
    head: tuple[str, ...] = ("Role", "Decision", "Stored level", "Proposed", "As is", "Small edits")
    head += (
        "Errors",
        "Unknown edits",
        "Rejected",
        "Waiting",
        "Unchanged/proposals",
        "Error-free/approved",
    )
    table = [head] + [
        (
            r.role,
            r.decision_type or "(none)",
            "default" if r.level is None else str(r.level),
            str(r.proposals),
            str(r.approved_unchanged),
            str(r.approved_cosmetic),
            str(r.approved_with_errors),
            str(r.approved_unknown),
            str(r.rejected),
            str(r.pending),
            _pct(r.approval_without_edit_rate),
            _pct(r.error_free_rate),
        )
        for r in rep.decision_types
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    for row in table:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)
