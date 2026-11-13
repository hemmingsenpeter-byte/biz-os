"""Golden cases as YAML; score a task on a tier; compare tiers; report the promotion gate.

Case file keys: name, system | system_file (relative to the case file), messages | prompt,
untrusted (list of text), schema | schema_file, expected (dict), checks (list). Other keys
(tags, why) are for people and ignored. With no checks a case gets one json_schema check.

A case passes when every one of its checks passes. Check types:

- json_schema: the output matches the case schema.
- exact {fields}: each listed field (default: every key of `expected`) equals `expected`.
- contains {field, value}: the field, as text, contains value (case-insensitive).
- numeric {field, value?, tolerance}: the field is within tolerance of value (default: the
  field's `expected` value).
- min {field, value, exclusive?}: the field is a number >= value (> when exclusive: true).
- max {field, value, exclusive?}: the field is a number <= value (< when exclusive: true).
- not_equal {field, value}: the field is not equal to value. A missing field passes.
- not_contains {field, value}: the field, as text, does not contain value (case-insensitive).
  A missing field passes.
- any_of {checks}: passes when at least one listed sub-check passes. "Category spam, or
  confidence below 0.6" is
      {type: any_of, checks: [{type: exact, fields: [category]},
                              {type: max, field: confidence, value: 0.6, exclusive: true}]}

`load_cases` rejects unknown check types and checks missing their keys.

Optional `_task.yaml` in the cases dir (files starting with `_` are not cases):
    threshold: 0.05                          # shadow: local may trail cloud's mean score by this
    gate: {pass_rate: 0.95, min_cases: 50}   # promotion gate (docs/PLAN.md, Measurement)

Reports give `passed`, `total` and `pass_rate` (share of cases passed) and a `gate` verdict:
the gate passes when pass_rate >= gate.pass_rate on at least gate.min_cases cases. A model
call that fails counts as a failed case. `score` (mean share of checks passed per case) is
kept for compatibility; the gate never uses it.
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
DEFAULT_GATE_PASS_RATE = 0.95
DEFAULT_GATE_MIN_CASES = 50
_EPS = 1e-9  # 57/60 must count as 0.95

# check type -> keys it must have
CHECK_KEYS: dict[str, tuple[str, ...]] = {
    "json_schema": (),
    "exact": (),
    "contains": ("field", "value"),
    "numeric": ("field",),
    "min": ("field", "value"),
    "max": ("field", "value"),
    "not_equal": ("field", "value"),
    "not_contains": ("field", "value"),
    "any_of": ("checks",),
}


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
        return self.error is None and self.score >= 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": self.score,
            "failures": self.failures,
            "output": self.output,
            "error": self.error,
        }


@dataclass(frozen=True)
class Gate:
    """Promotion gate: at least `pass_rate` of at least `min_cases` held-out cases pass."""

    pass_rate: float = DEFAULT_GATE_PASS_RATE
    min_cases: int = DEFAULT_GATE_MIN_CASES

    def verdict(self, passed: int, total: int) -> GateVerdict:
        rate = passed / total if total else 0.0
        reasons = []
        if total < self.min_cases:
            reasons.append(f"only {total} cases, the gate needs at least {self.min_cases}")
        if rate + _EPS < self.pass_rate:
            reasons.append(f"pass rate {rate:.1%} is below {self.pass_rate:.1%}")
        ok = not reasons
        if ok:
            reasons.append(
                f"{passed} of {total} cases passed ({rate:.1%}); "
                f"needs {self.pass_rate:.1%} of at least {self.min_cases}"
            )
        return GateVerdict(ok, "; ".join(reasons), self.pass_rate, self.min_cases)


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    reason: str
    min_pass_rate: float
    min_cases: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "min_pass_rate": self.min_pass_rate,
            "min_cases": self.min_cases,
        }


@dataclass
class EvalReport:
    task: str
    tier: Tier
    score: float
    results: list[CaseResult]
    rule: Gate = field(default_factory=Gate)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def failed(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    @property
    def gate(self) -> GateVerdict:
        return self.rule.verdict(self.passed, self.total)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "tier": self.tier,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "gate": self.gate.as_dict(),
            "score": self.score,
            "results": [r.as_dict() for r in self.results],
        }


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "local_pass_rate": self.local.pass_rate,
            "cloud_pass_rate": self.cloud.pass_rate,
            "agreement": self.agreement,
            "threshold": self.threshold,
            "ready_for_local": self.ready_for_local,
            "local": self.local.as_dict(),
            "cloud": self.cloud.as_dict(),
        }


@dataclass(frozen=True)
class TaskConfig:
    threshold: float = DEFAULT_THRESHOLD
    gate: Gate = field(default_factory=Gate)


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _validate_check(chk: Any, where: str) -> None:
    if not isinstance(chk, dict) or "type" not in chk:
        raise ValueError(f"{where}: a check must be a mapping with a type, got {chk!r}")
    kind = chk["type"]
    if kind not in CHECK_KEYS:
        raise ValueError(f"{where}: unknown check type {kind!r}")
    missing = [k for k in CHECK_KEYS[kind] if k not in chk]
    if missing:
        raise ValueError(f"{where}: {kind} check needs {', '.join(missing)}")
    if kind == "any_of":
        subs = chk["checks"]
        if not isinstance(subs, list) or not subs:
            raise ValueError(f"{where}: any_of needs a non-empty list of checks")
        for sub in subs:
            _validate_check(sub, where)


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
        checks = d.get("checks") or [{"type": "json_schema"}]
        for chk in checks:
            _validate_check(chk, f.name)
        out.append(
            Case(
                d.get("name", f.stem),
                system,
                messages,
                schema,
                d.get("expected") or {},
                checks,
                [str(u) for u in d.get("untrusted") or []],
            )
        )
    return out


def load_task_config(cases: Path | str | None) -> TaskConfig:
    """Shadow threshold and promotion gate from `_task.yaml`, with defaults."""
    if cases is None or not (Path(cases) / "_task.yaml").exists():
        return TaskConfig()
    d = _read(Path(cases) / "_task.yaml") or {}
    g = d.get("gate") or {}
    return TaskConfig(
        float(d.get("threshold", DEFAULT_THRESHOLD)),
        Gate(
            float(g.get("pass_rate", DEFAULT_GATE_PASS_RATE)),
            int(g.get("min_cases", DEFAULT_GATE_MIN_CASES)),
        ),
    )


def _threshold(cases: Path | str | None) -> float:
    return load_task_config(cases).threshold


def _number(out: dict[str, Any], f: str) -> float | None:
    v = out.get(f)
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


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
    elif kind == "not_contains":
        if str(chk["value"]).lower() in str(out.get(chk["field"], "")).lower():
            return f"not_contains: {chk['field']} contains {chk['value']!r}"
    elif kind == "not_equal":
        if chk["field"] in out and out[chk["field"]] == chk["value"]:
            return f"not_equal: {chk['field']} = {chk['value']!r}"
    elif kind in ("min", "max"):
        f, bound, strict = chk["field"], float(chk["value"]), bool(chk.get("exclusive"))
        got = _number(out, f)
        if got is None:
            return f"{kind}: {f} missing or not a number"
        if kind == "min" and (got < bound or (strict and got == bound)):
            return f"min: {f} = {got} is not {'above' if strict else 'at least'} {bound}"
        if kind == "max" and (got > bound or (strict and got == bound)):
            return f"max: {f} = {got} is not {'below' if strict else 'at most'} {bound}"
    elif kind == "numeric":
        f, tol = chk["field"], float(chk.get("tolerance", 0))
        want = chk.get("value", c.expected.get(f))
        try:
            if abs(float(out[f]) - float(want)) > tol:  # type: ignore[arg-type]
                return f"numeric: {f} = {out[f]} not within {tol} of {want}"
        except (KeyError, TypeError, ValueError):
            return f"numeric: {f} missing or not a number"
    elif kind == "any_of":
        fails = []
        for sub in chk["checks"]:
            msg = _check(c, out, sub)
            if msg is None:
                return None
            fails.append(msg)
        return f"any_of: none passed ({' | '.join(fails)})"
    else:
        return f"unknown check {kind}"
    return None


def score_case(c: Case, out: dict[str, Any]) -> CaseResult:
    fails = [m for chk in c.checks if (m := _check(c, out, chk))]
    total = len(c.checks) or 1
    return CaseResult(c.name, (total - len(fails)) / total, fails, out)


async def run_eval(
    task: str,
    tier: Tier,
    router: Router,
    cases: Path | str | list[Case],
    gate: Gate | None = None,
) -> EvalReport:
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    if gate is None:
        gate = load_task_config(None if isinstance(cases, list) else cases).gate
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
    return EvalReport(task, tier, score, results, gate)


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
    gate: Gate | None = None,
) -> ShadowReport:
    """Run both tiers on the same cases; report each tier's pass rate and gate, agreement,
    and readiness for local."""
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    config = load_task_config(None if isinstance(cases, list) else cases)
    gate = gate or config.gate
    local = await run_eval(task, "local", router, loaded, gate)
    cloud = await run_eval(task, "cloud", router, loaded, gate)
    agree = [_agree(c, a, b) for c, a, b in zip(loaded, local.results, cloud.results, strict=True)]
    if threshold is None:
        threshold = config.threshold
    return ShadowReport(task, local, cloud, sum(agree) / len(agree) if agree else 0.0, threshold)


def report_json(r: EvalReport | ShadowReport) -> str:
    return json.dumps(r.as_dict(), default=str, indent=2)


def report_text(r: EvalReport | ShadowReport) -> str:
    """Plain summary: pass counts, gate verdict, and each failing case with why."""
    if isinstance(r, ShadowReport):
        lines = [
            f"{r.task} [shadow]: agreement {r.agreement:.2f}, "
            f"ready_for_local={r.ready_for_local} (mean score local {r.local.score:.2f}, "
            f"cloud {r.cloud.score:.2f}, threshold {r.threshold:.2f})"
        ]
        for rep in (r.local, r.cloud):
            lines += ["", *_tier_lines(rep)]
        return "\n".join(lines)
    return "\n".join(_tier_lines(r))


def _tier_lines(r: EvalReport) -> list[str]:
    g = r.gate
    lines = [
        f"{r.task} [{r.tier}]: {r.passed} of {r.total} cases passed ({r.pass_rate:.1%}); "
        f"mean score {r.score:.2f}",
        f"gate: {'PASS' if g.passed else 'FAIL'} - {g.reason}",
    ]
    if r.failed:
        lines.append("failing cases:")
        lines += [f"  {f.name}: {'; '.join(f.failures) or 'failed'}" for f in r.failed]
    return lines
