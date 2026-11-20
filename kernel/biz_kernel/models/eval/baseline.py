"""Summarize owner-entered timing and volume measurements without identifiers."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

from biz_kernel.models.eval.private import OPAQUE_ID, csv_rows, private_path

FIELDS = ("sample_id", "owner_seconds", "approval_seconds")


def _duration(value: str, *, positive: bool) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise ValueError("measurements must be finite seconds") from exc
    if not math.isfinite(duration) or duration < 0 or (positive and duration == 0):
        raise ValueError("measurements must be finite and owner task time must be positive")
    return duration


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean_seconds": mean(values) if values else None,
        "median_seconds": median(values) if values else None,
    }


def baseline(
    measurements: Path, week_start: date, week_end: date, weekly_enquiries: int
) -> dict[str, Any]:
    if (week_end - week_start).days != 7 or weekly_enquiries < 0:
        raise ValueError("provide a complete observed week and a nonnegative enquiry count")
    rows = csv_rows(private_path(measurements), FIELDS)
    sample_ids = {row["sample_id"] for row in rows}
    if (
        len(rows) < 20
        or len(rows) != len(sample_ids)
        or any(not OPAQUE_ID.fullmatch(sample_id) for sample_id in sample_ids)
    ):
        raise ValueError("record at least 20 owner tasks with unique opaque sample IDs")
    owner_times = [_duration(row["owner_seconds"], positive=True) for row in rows]
    review_times = [
        _duration(row["approval_seconds"], positive=False)
        for row in rows
        if row["approval_seconds"].strip()
    ]
    return {
        "schema_version": 1,
        "week_start_inclusive": week_start.isoformat(),
        "week_end_exclusive": week_end.isoformat(),
        "weekly_enquiries": weekly_enquiries,
        "owner": _summary(owner_times),
        "approval_review": _summary(review_times),
        "baseline_workload_hours_per_week": mean(owner_times) * weekly_enquiries / 3600,
    }
