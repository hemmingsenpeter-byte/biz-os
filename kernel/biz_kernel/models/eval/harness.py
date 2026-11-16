"""Golden cases as YAML; score a task on a tier; compare tiers in shadow mode.

Case file keys: name, system | system_file (relative to the case file), messages | prompt,
untrusted (list of text), schema | schema_file, expected (dict), checks (list).
Check types: exact {fields}, contains {field, value}, json_schema, numeric {field, tolerance}.
Optional `_task.yaml` in the cases dir: {threshold: 0.05}.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
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
    passed_cases: int = 0
    total_cases: int = 0
    check_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.score

    @property
    def technical_gate_passed(self) -> bool:
        return (
            self.metadata.get("split") == "heldout"
            and self.metadata.get("outcome_checks_complete") is True
            and self.total_cases >= max(50, self.metadata.get("min_cases", 50))
            and self.pass_rate >= max(0.95, self.metadata.get("required_pass_rate", 0.95))
        )

    @property
    def deployment_ready(self) -> bool:
        return (
            self.technical_gate_passed
            and self.metadata.get("owner_reviewed") is True
            and self.metadata.get("provenance") in {"historical", "mixed"}
            and bool(self.metadata.get("configured_model"))
        )


@dataclass
class ShadowReport:
    task: str
    local: EvalReport
    cloud: EvalReport
    agreement: float
    threshold: float

    @property
    def ready_for_local(self) -> bool:
        return self.local.deployment_ready and self.technical_gate_passed

    @property
    def comparison_valid(self) -> bool:
        return (
            bool(self.local.results)
            and len(self.local.results) == len(self.cloud.results)
            and all(result.error is None for result in self.local.results + self.cloud.results)
        )

    @property
    def technical_gate_passed(self) -> bool:
        return (
            self.comparison_valid
            and self.local.technical_gate_passed
            and self.local.pass_rate >= self.cloud.pass_rate - self.threshold
        )


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def load_cases(cases: Path | str) -> list[Case]:
    if not Path(cases).is_dir():
        raise ValueError(f"case directory does not exist: {cases}")
    out = []
    for f in sorted(Path(cases).glob("*.y*ml")):
        if f.name.startswith("_"):
            continue
        d = _read(f)
        if not isinstance(d, dict):
            raise ValueError(f"invalid case mapping: {f.name}")
        system = d.get("system") or ""
        if d.get("system_file"):
            system = (f.parent / d["system_file"]).read_text()
        schema = d.get("schema")
        if d.get("schema_file"):
            schema = _read(f.parent / d["schema_file"])
        if not isinstance(schema, dict):
            raise ValueError(f"case {f.name} needs a JSON schema")
        jsonschema.Draft202012Validator.check_schema(schema)
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
                d.get("checks")
                or (
                    [{"type": "json_schema"}, {"type": "exact"}]
                    if d.get("expected")
                    else [{"type": "json_schema"}]
                ),
                [str(u) for u in d.get("untrusted") or []],
            )
        )
    if not out:
        raise ValueError(f"no evaluation cases found: {cases}")
    return out


def dataset_metadata(cases: Path | str | list[Case], loaded: list[Case]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if not isinstance(cases, list) and (Path(cases) / "_task.yaml").is_file():
        raw = _read(Path(cases) / "_task.yaml")
        if not isinstance(raw, dict):
            raise ValueError("_task.yaml must be a mapping")
        data = dict(raw)
    rate, minimum = data.get("required_pass_rate", 0.95), data.get("min_cases", 50)
    if (
        isinstance(rate, bool)
        or not isinstance(rate, int | float)
        or not math.isfinite(rate)
        or not 0 < rate <= 1
    ):
        raise ValueError("required_pass_rate must be finite and in (0, 1]")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("min_cases must be a positive integer")
    data.update(required_pass_rate=float(rate), min_cases=minimum)
    payload = json.dumps([asdict(case) for case in loaded], sort_keys=True).encode()
    prompts = json.dumps(
        [{"system": c.system, "schema": c.schema} for c in loaded], sort_keys=True
    ).encode()
    data["dataset_sha256"] = hashlib.sha256(payload).hexdigest()
    data["prompt_schema_sha256"] = hashlib.sha256(prompts).hexdigest()
    # A valid JSON shape alone cannot establish a correct decision. Never trust a
    # manifest's assertion about coverage; derive it from the loaded checks.
    data["outcome_checks_complete"] = all(_has_outcome_check(case) for case in loaded)
    return data


def _has_outcome_check(case: Case) -> bool:
    covered: set[str] = set()
    for check in case.checks:
        kind = check.get("type")
        if kind == "exact":
            covered.update(f for f in check.get("fields") or case.expected if f in case.expected)
        elif (
            kind in {"contains", "numeric"}
            and "field" in check
            and ("value" in check or check["field"] in case.expected)
        ):
            covered.add(check["field"])
    if case.expected:
        return set(case.expected) <= covered
    return bool(covered)


def _threshold(cases: Path | str | None) -> float:
    if cases is not None and (Path(cases) / "_task.yaml").exists():
        value = _read(Path(cases) / "_task.yaml").get("threshold", DEFAULT_THRESHOLD)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("shadow threshold must be finite and in [0, 1]")
        return float(value)
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
    elif kind == "numeric_min":
        value, minimum = out.get(chk["field"]), chk.get("min")
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or isinstance(minimum, bool)
            or not isinstance(minimum, int | float)
            or not math.isfinite(value)
            or not math.isfinite(minimum)
            or value < minimum
        ):
            return f"numeric_min: {chk['field']} must be at least {minimum}"
    elif kind == "numeric":
        f, tol = chk["field"], float(chk.get("tolerance", 0))
        want = chk.get("value", c.expected.get(f))
        try:
            actual, expected = float(out[f]), float(want)  # type: ignore[arg-type]
            if (
                not all(math.isfinite(v) for v in (actual, expected, tol))
                or tol < 0
                or abs(actual - expected) > tol
            ):
                return f"numeric: {f} = {out[f]} not within {tol} of {want}"
        except (KeyError, TypeError, ValueError):
            return f"numeric: {f} missing or not a number"
    else:
        return f"unknown check {kind}"
    return None


def score_case(c: Case, out: dict[str, Any]) -> CaseResult:
    try:
        json.dumps(out, allow_nan=False)
    except (ValueError, TypeError):
        return CaseResult(c.name, 0.0, ["output is not finite JSON"], error="invalid JSON output")
    if not c.checks:
        return CaseResult(c.name, 0.0, ["case has no checks"], out)
    fails = [m for chk in c.checks if (m := _check(c, out, chk))]
    total = len(c.checks) or 1
    return CaseResult(c.name, (total - len(fails)) / total, fails, out)


async def run_eval(
    task: str, tier: Tier, router: Router, cases: Path | str | list[Case]
) -> EvalReport:
    if tier not in ("local", "cloud"):
        raise ValueError(f"invalid evaluation tier: {tier}")
    port = router.cloud if tier == "cloud" else router.local
    if port is None:
        raise ValueError(f"requested {tier} model is not configured")
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    if not loaded:
        raise ValueError("evaluation needs at least one case")
    metadata = dataset_metadata(cases, loaded)
    if metadata.get("task") not in (None, task):
        raise ValueError(f"dataset is for {metadata['task']}, not {task}")
    metadata.update(
        actual_tier=tier,
        backend=f"{type(port).__module__}.{type(port).__name__}",
        configured_model=getattr(port, "model", None),
    )
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
    passed = sum(result.passed for result in results)
    return EvalReport(
        task,
        tier,
        passed / len(results),
        results,
        passed,
        len(results),
        sum(r.score for r in results) / len(results),
        metadata,
    )


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
    if router.cloud is None:
        raise ValueError("shadow evaluation requires a configured cloud model")
    metadata = dataset_metadata(cases, loaded)
    if metadata.get("task") not in (None, task):
        raise ValueError(f"dataset is for {metadata['task']}, not {task}")
    if threshold is None:
        threshold = _threshold(None if isinstance(cases, list) else cases)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("shadow threshold must be finite and in [0, 1]")
    local = await run_eval(task, "local", router, loaded)
    cloud = await run_eval(task, "cloud", router, loaded)
    local.metadata = {
        **metadata,
        **{k: local.metadata[k] for k in ("actual_tier", "backend", "configured_model")},
    }
    cloud.metadata = {
        **metadata,
        **{k: cloud.metadata[k] for k in ("actual_tier", "backend", "configured_model")},
    }
    agree = [_agree(c, a, b) for c, a, b in zip(loaded, local.results, cloud.results, strict=True)]
    return ShadowReport(task, local, cloud, sum(agree) / len(agree) if agree else 0.0, threshold)


def report_json(r: EvalReport | ShadowReport) -> str:
    def evaluation(value: EvalReport) -> dict[str, Any]:
        return {
            **asdict(value),
            "pass_rate": value.pass_rate,
            "technical_gate_passed": value.technical_gate_passed,
            "deployment_ready": value.deployment_ready,
        }

    if isinstance(r, ShadowReport):
        data = {
            "task": r.task,
            "local": evaluation(r.local),
            "cloud": evaluation(r.cloud),
            "agreement": r.agreement,
            "threshold": r.threshold,
            "comparison_valid": r.comparison_valid,
            "technical_gate_passed": r.technical_gate_passed,
            "ready_for_local": r.ready_for_local,
        }
    else:
        data = evaluation(r)
    return json.dumps(data, indent=2, allow_nan=False)
