"""Golden cases as YAML; score a task on a tier; compare tiers in shadow mode.

Case file keys: name, system | system_file (relative to the case file), messages | prompt,
untrusted (list of text), schema | schema_file, expected (dict), checks (list).
Check types: exact {fields}, contains {field, value}, json_schema, numeric {field, tolerance}.
Optional `_task.yaml` in the cases dir: {threshold: 0.05}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from biz_kernel.models.router import Router, Tier

DEFAULT_THRESHOLD = 0.05


@dataclass
class Case:
    name: str
    system: str
    messages: list[dict[str, Any]]
    schema: dict[str, Any]
    expected: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    name: str
    score: float
    failures: list[str]
    output: dict[str, Any] | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.score >= 1.0


@dataclass
class EvalReport:
    task: str
    tier: Tier
    score: float
    results: list[CaseResult]


@dataclass
class ShadowReport:
    task: str
    local: EvalReport
    cloud: EvalReport
    agreement: float
    threshold: float

    @property
    def ready_for_local(self) -> bool:
        return self.local.score >= self.cloud.score - self.threshold


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def load_cases(cases: Path | str) -> list[Case]:
    out = []
    for f in sorted(Path(cases).glob("*.y*ml")):
        if f.name.startswith("_"):
            continue
        d = _read(f)
        system = d.get("system") or ""
        if d.get("system_file"):
            system = (f.parent / d["system_file"]).read_text()
        schema = d.get("schema")
        if d.get("schema_file"):
            schema = _read(f.parent / d["schema_file"])
        messages = d.get("messages")
        if messages is None:
            messages = (
                [] if d.get("untrusted") else [{"role": "user", "content": d.get("prompt", "")}]
            )
        out.append(
            Case(
                d.get("name", f.stem),
                system,
                messages,
                schema,
                d.get("expected") or {},
                d.get("checks") or [{"type": "json_schema"}],
                [str(u) for u in d.get("untrusted") or []],
            )
        )
    return out


def _threshold(cases: Path | str | None) -> float:
    if cases is not None and (Path(cases) / "_task.yaml").exists():
        return float(_read(Path(cases) / "_task.yaml").get("threshold", DEFAULT_THRESHOLD))
    return DEFAULT_THRESHOLD


def _check(c: Case, out: dict[str, Any], chk: dict[str, Any]) -> str | None:
    kind = chk["type"]
    if kind == "json_schema":
        try:
            jsonschema.validate(out, c.schema)
        except jsonschema.ValidationError as e:
            return f"json_schema: {e.message}"
    elif kind == "exact":
        for f in chk.get("fields") or list(c.expected):
            if out.get(f) != c.expected.get(f):
                return f"exact: {f} = {out.get(f)!r}, expected {c.expected.get(f)!r}"
    elif kind == "contains":
        if str(chk["value"]).lower() not in str(out.get(chk["field"], "")).lower():
            return f"contains: {chk['field']} lacks {chk['value']!r}"
    elif kind == "numeric":
        f, tol = chk["field"], float(chk.get("tolerance", 0))
        want = chk.get("value", c.expected.get(f))
        try:
            if abs(float(out[f]) - float(want)) > tol:  # type: ignore[arg-type]
                return f"numeric: {f} = {out[f]} not within {tol} of {want}"
        except (KeyError, TypeError, ValueError):
            return f"numeric: {f} missing or not a number"
    else:
        return f"unknown check {kind}"
    return None


def score_case(c: Case, out: dict[str, Any]) -> CaseResult:
    fails = [m for chk in c.checks if (m := _check(c, out, chk))]
    total = len(c.checks) or 1
    return CaseResult(c.name, (total - len(fails)) / total, fails, out)


async def run_eval(
    task: str, tier: Tier, router: Router, cases: Path | str | list[Case]
) -> EvalReport:
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    results = []
    for c in loaded:
        try:
            r = await router.generate(
                task,
                c.system,
                c.messages,
                c.schema,
                tier_hint=tier,
                untrusted=c.untrusted or None,
                escalate=False,
            )
            results.append(score_case(c, r.output))
        except Exception as exc:  # a failed call scores zero, the run continues
            results.append(CaseResult(c.name, 0.0, [str(exc)], error=str(exc)))
    score = sum(r.score for r in results) / len(results) if results else 0.0
    return EvalReport(task, tier, score, results)


def _agree(c: Case, a: CaseResult, b: CaseResult) -> bool:
    if a.output is None or b.output is None:
        return False
    keys = list(c.expected) or list(a.output)
    return all(a.output.get(k) == b.output.get(k) for k in keys)


async def shadow(
    task: str,
    router: Router,
    cases: Path | str | list[Case],
    threshold: float | None = None,
) -> ShadowReport:
    """Run both tiers on the same cases and report agreement and readiness for local."""
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    local = await run_eval(task, "local", router, loaded)
    cloud = await run_eval(task, "cloud", router, loaded)
    agree = [_agree(c, a, b) for c, a, b in zip(loaded, local.results, cloud.results, strict=True)]
    if threshold is None:
        threshold = _threshold(None if isinstance(cases, list) else cases)
    return ShadowReport(task, local, cloud, sum(agree) / len(agree) if agree else 0.0, threshold)


def report_json(r: EvalReport | ShadowReport) -> str:
    return json.dumps(r, default=lambda o: getattr(o, "__dict__", str(o)), indent=2)
