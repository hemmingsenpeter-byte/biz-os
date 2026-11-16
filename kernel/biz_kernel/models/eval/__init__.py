"""Evaluation harness: golden cases, per-tier scoring, shadow-mode comparison."""

from biz_kernel.models.eval.harness import (
    Case,
    CaseResult,
    EvalReport,
    ShadowReport,
    load_cases,
    run_eval,
    shadow,
)

__all__ = [
    "Case",
    "CaseResult",
    "EvalReport",
    "ShadowReport",
    "load_cases",
    "run_eval",
    "shadow",
]
