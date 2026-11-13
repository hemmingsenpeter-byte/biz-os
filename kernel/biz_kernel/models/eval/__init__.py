"""Evaluation harness: golden cases, per-tier scoring, shadow mode, promotion gate."""

from biz_kernel.models.eval.harness import (
    Case,
    CaseResult,
    EvalReport,
    Gate,
    GateVerdict,
    ShadowReport,
    TaskConfig,
    load_cases,
    load_task_config,
    report_json,
    report_text,
    run_eval,
    score_case,
    shadow,
)

__all__ = [
    "Case",
    "CaseResult",
    "EvalReport",
    "Gate",
    "GateVerdict",
    "ShadowReport",
    "TaskConfig",
    "load_cases",
    "load_task_config",
    "report_json",
    "report_text",
    "run_eval",
    "score_case",
    "shadow",
]
