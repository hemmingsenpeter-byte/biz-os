"""Golden cases as YAML; score a task on a tier; compare tiers in shadow mode.

Case file keys: name, system | system_file (relative to the case file), messages | prompt,
untrusted (list of text), schema | schema_file, expected (dict), checks (list).
Check types: exact {fields}, contains {field, value}, json_schema, numeric {field, tolerance}.
Optional `_task.yaml` in the cases dir: {threshold: 0.05}.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import jsonschema
import yaml

from biz_kernel.models.eval.private import (
    DEFAULT_THRESHOLD,
    OPAQUE_ID,
    CloudAuthorisation,
    find_repo_root,
    private_path,
    read_template,
    record_cloud_disclosure,
    scoring_contract_sha256,
    sha_json,
    sha_text,
)
from biz_kernel.models.router import RoutedResult, Router, Tier, spent_before

REAL_BACKEND: dict[Tier, str] = {
    "local": "biz_port_model_ollama.OllamaModel",
    "cloud": "biz_port_model_claude.ClaudeModel",
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
    # Where the case came from, set by load_cases, so the private gates apply however
    # the cases are passed in. A case without one never leaves the machine. Not part of
    # the dataset hash or of case equality.
    provenance: Provenance | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Provenance:
    kind: Literal["private", "public"]
    directory: str


def _hashed(case: Case) -> dict[str, Any]:
    data = asdict(case)
    del data["provenance"]
    return data


def dataset_sha256(loaded: list[Case]) -> str:
    payload = json.dumps([_hashed(case) for case in loaded], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass
class CaseResult:
    name: str
    score: float
    failures: list[str]
    output: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: float | None = None
    usage: dict[str, int | str] | None = None

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
            and self.metadata.get("provenance") in ("historical", "mixed")
            and self.metadata.get("prompt", {}).get("matches_deployed") is True
            and self.metadata.get("scoring_contract", {}).get("matches_deployed") is True
            and self.has_model_evidence
        )

    @property
    def has_model_evidence(self) -> bool:
        return (
            self.metadata.get("backend") == REAL_BACKEND.get(self.tier)
            and bool(self.metadata.get("configured_model"))
            and bool(self.metadata.get("model_version"))
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
        return (
            self.local.deployment_ready
            and self.cloud.has_model_evidence
            and self.technical_gate_passed
        )

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
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid evaluation YAML in {path.name}") from exc


def is_private(data: dict[str, Any]) -> bool:
    if "private" in data and type(data["private"]) is not bool:
        raise ValueError("evaluation private flag must be a boolean")
    return data.get("private") is True or data.get("provenance") in ("historical", "mixed")


def read_task(cases: Path | str) -> dict[str, Any]:
    directory = Path(cases)
    path = directory / "_task.yaml"
    raw = _read(path) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError("_task.yaml must be a mapping")
    data = dict(raw)
    if (directory / "_owner_review.json").is_file():
        data["private"] = True
    return data


def load_cases(cases: Path | str) -> list[Case]:
    if not Path(cases).is_dir():
        raise ValueError(f"case directory does not exist: {cases}")
    private_dataset = is_private(read_task(cases))
    directory = str(Path(cases).resolve())
    out = []
    for f in sorted(Path(cases).glob("*.y*ml")):
        if f.name.startswith("_"):
            continue
        d = _read(f)
        if not isinstance(d, dict):
            raise ValueError(f"invalid case mapping: {f.name}")
        # A standalone exported case can carry customer material without a dataset
        # manifest. Its privacy flag can only tighten the enclosing dataset's status.
        private_case = is_private(d)
        provenance = Provenance(
            "private" if private_dataset or private_case else "public", directory
        )
        system = d.get("system") or ""
        if d.get("system_file"):
            system = (f.parent / d["system_file"]).read_text()
        schema = d.get("schema")
        if d.get("schema_file"):
            schema = _read(f.parent / d["schema_file"])
        if not isinstance(schema, dict):
            raise ValueError(f"case {f.name} needs a JSON schema")
        jsonschema.validators.validator_for(schema).check_schema(schema)
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
                provenance,
            )
        )
    if not out:
        raise ValueError(f"no evaluation cases found: {cases}")
    return out


def _is_private_dir(directory: str) -> bool:
    try:
        return Path(directory).is_dir() and is_private(read_task(directory))
    except ValueError:
        return True  # an unreadable manifest cannot vouch for public data


def _private_origin(loaded: list[Case]) -> Path | None:
    """The private dataset the cases came from, if any.

    A case marked public whose directory is a private dataset counts as private; its
    content must then match that dataset (checked in dataset_metadata).
    """
    marked = [c.provenance for c in loaded if c.provenance is not None]
    directories = {p.directory for p in marked}
    private_dirs = {p.directory for p in marked if p.kind == "private"}
    private_dirs |= {d for d in directories - private_dirs if _is_private_dir(d)}
    if not private_dirs:
        return None
    if (
        len(private_dirs) > 1
        or directories != private_dirs
        or any(c.provenance is None for c in loaded)
    ):
        raise ValueError("cases must not mix a private dataset with other cases")
    return Path(next(iter(private_dirs)))


def _require_provenance(loaded: list[Case]) -> None:
    if any(case.provenance is None for case in loaded):
        raise ValueError("cases sent off this machine need a provenance; load them with load_cases")


def dataset_metadata(
    cases: Path | str | list[Case], loaded: list[Case], *, repo_root: Path | None = None
) -> dict[str, Any]:
    origin = _private_origin(loaded)
    if origin is not None and (isinstance(cases, list) or Path(cases).resolve() != origin):
        # Private cases passed as a list: verify them as their confirmed dataset.
        full = load_cases(origin)
        if any(case not in full for case in loaded):
            raise ValueError("private cases differ from their confirmed dataset")
        return dataset_metadata(origin, full, repo_root=repo_root)
    data = {} if isinstance(cases, list) else read_task(cases)
    if origin is not None:
        # A per-case declaration also requires the existing private cohort receipt
        # and integrity checks; ad-hoc previews are not confirmed evaluation datasets.
        data["private"] = True
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
    prompts = json.dumps(
        [{"system": c.system, "schema": c.schema} for c in loaded], sort_keys=True
    ).encode()
    data["dataset_sha256"] = dataset_sha256(loaded)
    data["prompt_schema_sha256"] = hashlib.sha256(prompts).hexdigest()
    # A valid JSON shape alone cannot establish a correct decision. Never trust a
    # manifest's assertion about coverage; derive it from the loaded checks.
    data["outcome_checks_complete"] = all(_has_outcome_check(case) for case in loaded)
    data["owner_reviewed"] = False
    prompts_scored = {case.system for case in loaded}
    scored = sha_text(next(iter(prompts_scored))) if len(prompts_scored) == 1 else None
    data["prompt"] = {
        "scored_sha256": scored,
        "deployed_path": None,
        "deployed_sha256": None,
        "matches_deployed": None,
    }
    if is_private(data):
        assert not isinstance(cases, list)
        receipt = _verify_owner_review(private_path(Path(cases)), data, loaded)
        data["review"] = {
            key: receipt[key] for key in ("owner", "confirmed_at", "case_count", "source_sha256")
        }
        data["owner_reviewed"] = True
        data["prompt"] = _prompt_status(receipt, scored, repo_root)
        data["scoring_contract"] = _scoring_contract_status(receipt, repo_root)
    return data


def _scoring_contract_status(receipt: dict[str, Any], repo_root: Path | None) -> dict[str, Any]:
    """Older contracts remain scoreable, but only the current one establishes readiness."""
    source, scored = receipt.get("template_source"), receipt.get("scoring_contract_sha256")
    root = repo_root or find_repo_root(Path.cwd())
    deployed = None
    if isinstance(source, str) and root is not None:
        try:
            deployed = scoring_contract_sha256(read_template((root / source).parent))
        except (OSError, ValueError):
            deployed = None
    return {
        "scored_sha256": scored,
        "deployed_path": source,
        "deployed_sha256": deployed,
        "matches_deployed": None if deployed is None else deployed == scored,
    }


def _prompt_status(
    receipt: dict[str, Any], scored: str | None, repo_root: Path | None
) -> dict[str, Any]:
    """The frozen prompt that is scored, and whether it is the one deployed now."""
    source = receipt.get("prompt_source")
    root = repo_root or find_repo_root(Path.cwd())
    deployed = None
    if isinstance(source, str) and root is not None:
        try:
            deployed = sha_text((root / source).read_text())
        except OSError:
            deployed = None
    return {
        "scored_sha256": scored,
        "deployed_path": source if isinstance(source, str) else None,
        "deployed_sha256": deployed,
        "matches_deployed": None if deployed is None else deployed == scored,
    }


def _verify_owner_review(
    directory: Path, data: dict[str, Any], cases: list[Case]
) -> dict[str, Any]:
    path = directory / "_owner_review.json"
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("private dataset requires a readable owner review receipt") from exc
    keys = (
        "schema_version",
        "dataset",
        "dataset_version",
        "task",
        "decision_type",
        "split",
        "provenance",
        "private",
        "dataset_sha256",
        "threshold",
    )
    if (
        not isinstance(receipt, dict)
        or any(key not in receipt or receipt[key] != data.get(key) for key in keys)
        or receipt.get("case_count") != len(cases)
        or not isinstance(receipt.get("owner"), str)
        or not OPAQUE_ID.fullmatch(receipt["owner"])
        or not isinstance(receipt.get("source_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"])
        or len({case.name for case in cases}) != len(cases)
        or {sha_text(case.system) for case in cases} != {receipt.get("prompt_sha256")}
        or any(
            receipt.get(key) != data.get(key)
            for key in ("scoring_contract_sha256", "template_source")
        )
        or (
            receipt.get("scoring_contract_sha256") is not None
            and any(
                receipt.get(key) != data.get(key) for key in ("required_pass_rate", "min_cases")
            )
        )
    ):
        raise ValueError("owner review receipt does not match the labelled dataset")
    _verify_review_split(directory, data, receipt, cases)
    try:
        if datetime.fromisoformat(receipt["confirmed_at"]).utcoffset() is None:
            raise ValueError("confirmation needs a timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("owner review receipt must include a dated confirmation") from exc
    return receipt


def _verify_review_split(
    directory: Path, data: dict[str, Any], receipt: dict[str, Any], cases: list[Case]
) -> None:
    split = data.get("split")
    version_dir = directory.parent.parent
    if (
        split not in ("tuning", "heldout")
        or directory.name != split
        or directory.parent.name != "confirmed"
        or version_dir.name != data.get("dataset_version")
    ):
        raise ValueError("evaluate the canonical confirmed dataset in its original split directory")
    try:
        prepared = json.loads((version_dir / "prepared.json").read_text())
        ledger = json.loads((version_dir.parent / "splits.json").read_text())["splits"]
        members = {case_id for case_id in prepared if ledger[case_id] == split}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("canonical split evidence is missing or invalid") from exc
    if sha_json(prepared) != receipt["source_sha256"] or members != {c.name for c in cases}:
        raise ValueError("reviewed cases do not match their reserved cohort and split")


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


REAL_ADAPTERS = {
    REAL_BACKEND["local"]: "biz-port-model-ollama",
    REAL_BACKEND["cloud"]: "biz-port-model-claude",
}


def _adapter_version(backend: str) -> str | None:
    distribution = REAL_ADAPTERS.get(backend)
    if distribution is None:
        return None
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


async def run_eval(
    task: str,
    tier: Tier,
    router: Router,
    cases: Path | str | list[Case],
    *,
    model_version: str | None = None,
    cloud_data: CloudAuthorisation | None = None,
    repo_root: Path | None = None,
) -> EvalReport:
    """Score `cases` on `tier`. Private cases reach a cloud model only with `cloud_data`."""
    if tier not in ("local", "cloud"):
        raise ValueError(f"invalid evaluation tier: {tier}")
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    if not loaded:
        raise ValueError("evaluation needs at least one case")
    metadata = dataset_metadata(cases, loaded, repo_root=repo_root)
    if metadata.get("task") not in (None, task):
        raise ValueError(f"dataset is for {metadata['task']}, not {task}")
    if tier == "cloud":
        _require_cloud_authorisation(metadata, cloud_data)
    workspace = _workspace_of(cases, metadata)
    return await _run_loaded(
        task, tier, router, loaded, metadata, model_version, cloud_data, workspace
    )


def _workspace_of(cases: Path | str | list[Case], metadata: dict[str, Any]) -> Path | None:
    if not is_private(metadata):
        return None
    if isinstance(cases, list):
        origin = _private_origin(cases)
        if origin is None:
            return None
        cases = origin
    # The canonical layout, checked with the owner review: <workspace>/<version>/confirmed/<split>
    return private_path(Path(cases)).parents[2]


def loopback_ollama_url(url: str, model: str) -> str:
    """The direct loopback form of an Ollama URL; ValueError unless loopback and noncloud."""
    problem = "private local evaluation requires a loopback Ollama URL and a noncloud model"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError(problem)
    except ValueError as exc:
        raise ValueError(problem) from exc
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.search(r"(?:^|[^a-z0-9])cloud(?:$|[^a-z0-9])", model.lower())
    ):
        raise ValueError(problem)
    if host == "localhost":
        # Use the actual loopback address rather than system name resolution.
        parsed = parsed._replace(netloc=f"127.0.0.1:{port}" if port else "127.0.0.1")
    return parsed.geturl()


def _keeps_data_local(port: Any) -> bool:
    backend = f"{type(port).__module__}.{type(port).__name__}"
    if backend.startswith("biz_port_fakes."):
        return True
    if backend != REAL_BACKEND["local"]:
        return False
    try:
        loopback_ollama_url(str(port.base_url), str(port.model))
    except (AttributeError, ValueError):
        return False
    return True


def _require_cloud_authorisation(metadata: dict[str, Any], cloud_data: Any) -> None:
    if is_private(metadata) and not isinstance(cloud_data, CloudAuthorisation):
        raise ValueError(
            "private evaluation data may be sent to a cloud model only with an "
            "authorisation from --allow-cloud-data, the app's policy and an operator"
        )


def _usage(r: RoutedResult) -> dict[str, int | str] | None:
    # An adapter that reported nothing leaves no usage record; a zero-filled one
    # would read as measured. Partly reported cases keep the known figures.
    if r.unmetered_calls >= r.attempts:
        return None
    return {
        **asdict(r.usage),
        "cost_usd": str(r.cost_usd),
        "attempts": r.attempts,
        "unmetered_calls": r.unmetered_calls,
    }


async def _run_loaded(
    task: str,
    tier: Tier,
    router: Router,
    loaded: list[Case],
    metadata: dict[str, Any],
    model_version: str | None,
    cloud_data: CloudAuthorisation | None = None,
    workspace: Path | None = None,
) -> EvalReport:
    port = router.cloud if tier == "cloud" else router.local
    if port is None:
        raise ValueError(f"requested {tier} model is not configured")
    backend = f"{type(port).__module__}.{type(port).__name__}"
    private = is_private(metadata)
    if _private_origin(loaded) is not None and not private:
        raise ValueError("private cases must be scored with their verified dataset metadata")
    # A private run leaves the machine on the cloud tier, or on a "local" port that is
    # not a loopback, noncloud Ollama model (or a fake). Either needs the authorisation.
    remote = tier == "cloud" or not _keeps_data_local(port)
    if remote:
        _require_provenance(loaded)
    elif private and backend == REAL_BACKEND["local"]:
        # Connect to the loopback address itself, not whatever `localhost` resolves to.
        direct = copy.copy(port)
        direct.base_url = loopback_ollama_url(port.base_url, port.model).rstrip("/")
        router = replace(router, local=direct)
    if private and remote and not isinstance(cloud_data, CloudAuthorisation) and tier == "local":
        raise ValueError(
            "private local evaluation requires a loopback Ollama URL and a noncloud model, "
            "or an authorisation for cloud data"
        )
    disclosure: dict[str, Any] | None = None
    if private and remote:
        _require_cloud_authorisation(metadata, cloud_data)
        assert cloud_data is not None
        if workspace is None:
            raise ValueError("a private cloud run needs its workspace for the audit record")
        # Recorded before the first call: if the audit cannot be written, nothing is sent.
        disclosure = {
            "event": "private_eval_cloud_disclosure",
            "at": datetime.now(UTC).isoformat(),
            "operator": cloud_data.operator,
            "app": cloud_data.app,
            "task": task,
            "dataset": metadata.get("dataset"),
            "dataset_version": metadata.get("dataset_version"),
            "split": metadata.get("split"),
            "dataset_sha256": metadata["dataset_sha256"],
            "source_sha256": metadata["review"]["source_sha256"],
            "dataset_owner": metadata["review"]["owner"],
            "provider": backend,
            "configured_model": getattr(port, "model", None),
            "model_version": model_version,
            "case_count": len(loaded),
            "tier": tier,
            "redacted": True,
        }
        record_cloud_disclosure(workspace, disclosure)
    if private:
        metadata["cloud_data_disclosure"] = disclosure
    results = []
    for c in loaded:
        start = time.perf_counter()
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
            result = score_case(c, r.output)
            result.usage = _usage(r)
        except Exception as exc:  # a failed call scores zero, the run continues
            result = CaseResult(c.name, 0.0, [str(exc)], error=str(exc))
            spent = spent_before(exc)
            if spent is not None:  # its failed attempts still cost money
                result.usage = _usage(spent)
        result.latency_ms = (time.perf_counter() - start) * 1000
        results.append(result)
    recorded_cost = sum(
        (Decimal(str(result.usage["cost_usd"])) for result in results if result.usage),
        Decimal(0),
    )
    metadata.update(
        actual_tier=tier,
        backend=backend,
        adapter_version=_adapter_version(backend),
        configured_model=getattr(port, "model", None),
        model_version=model_version,
        total_latency_ms=sum(result.latency_ms or 0 for result in results),
        recorded_model_api_cost_usd=str(recorded_cost),
        unmetered_cases=sum(
            result.usage is None or bool(result.usage.get("unmetered_calls")) for result in results
        ),
    )
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
    *,
    model_versions: Mapping[Tier, str] | None = None,
    cloud_data: CloudAuthorisation | None = None,
    repo_root: Path | None = None,
) -> ShadowReport:
    """Run both tiers on the same cases and report agreement and readiness for local."""
    loaded = cases if isinstance(cases, list) else load_cases(cases)
    if router.cloud is None:
        raise ValueError("shadow evaluation requires a configured cloud model")
    metadata = dataset_metadata(cases, loaded, repo_root=repo_root)
    if metadata.get("task") not in (None, task):
        raise ValueError(f"dataset is for {metadata['task']}, not {task}")
    _require_cloud_authorisation(metadata, cloud_data)  # before either tier runs
    if is_private(metadata) and threshold not in (None, metadata.get("threshold")):
        raise ValueError("a private dataset is compared at its owner-confirmed threshold")
    if threshold is None:
        threshold = _threshold(None if isinstance(cases, list) else cases)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("shadow threshold must be finite and in [0, 1]")
    versions = model_versions or {}
    workspace = _workspace_of(cases, metadata)
    local = await _run_loaded(
        task, "local", router, loaded, dict(metadata), versions.get("local"), cloud_data, workspace
    )
    cloud = await _run_loaded(
        task, "cloud", router, loaded, dict(metadata), versions.get("cloud"), cloud_data, workspace
    )
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
    disclosure = cloud_disclosure(r)
    data["private_data_sent_to_cloud"] = disclosure is not None
    data["cloud_data_disclosure"] = disclosure
    data["prompt"] = (r.local if isinstance(r, ShadowReport) else r).metadata.get("prompt")
    return json.dumps(data, indent=2, allow_nan=False)


def cloud_disclosure(r: EvalReport | ShadowReport) -> dict[str, Any] | None:
    """The audit record of private data this run sent to a cloud model, if it sent any."""
    report = r.cloud if isinstance(r, ShadowReport) else r
    disclosure = report.metadata.get("cloud_data_disclosure")
    return disclosure if isinstance(disclosure, dict) else None
