"""Durable host-side paired comparisons of owner-attested private tuning evidence.

This executor does not import historical evaluation receipts or grant readiness. Its
own training-purpose authorization is checked by cohorts.prepare_execution before
inference. Both sides are fresh Router calls scored by the ordinary case scorer.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from biz_port_model_ollama import OllamaModel
from dbos import DBOS, SetWorkflowID
from sqlalchemy import text

from biz_kernel import db
from biz_kernel.control import service as control
from biz_kernel.models import local_ollama
from biz_kernel.models import usage as usage_mod
from biz_kernel.models.eval import harness
from biz_kernel.models.router import Router, spent_before
from biz_kernel.training.service import TrainingError

# Bump only for an incompatible change to the durable step sequence. Each job also pins
# the exact implementation; changed inference/scoring code invalidates old evidence.
WORKFLOW_VERSION = "training-comparison-1"
SIDES = ("baseline", "candidate")
MODEL_FIELDS = {
    "backend",
    "name",
    "digest",
    "ollama_version",
    "backend_version",
    "base_url",
    "temperature",
    "seed",
    "num_predict",
    "timeout_seconds",
    "repetitions",
}


class ComparisonBusy(RuntimeError):
    """Another host worker currently owns this comparison's inference admission."""


def implementation_pins() -> dict[str, str]:
    """Pin all inference/scoring implementations, not only their package version labels."""
    from biz_port_model_ollama import config

    from biz_kernel.models import router

    modules = {
        "scorer_sha256": [Path(harness.__file__)],
        "router_sha256": [Path(router.__file__)],
        "adapter_sha256": [
            Path(config.__file__).with_name("__init__.py"),
            Path(config.__file__),
        ],
        "executor_sha256": [Path(__file__)],
        "verification_sha256": [Path(local_ollama.__file__)],
    }
    return {
        key: hashlib.sha256(b"\x00".join(path.read_bytes() for path in paths)).hexdigest()
        for key, paths in modules.items()
    }


def validate_model_settings(model: dict[str, Any]) -> None:
    if not isinstance(model, dict) or set(model) != MODEL_FIELDS:
        raise TrainingError("comparison model settings must contain the complete frozen contract")
    if model["backend"] != local_ollama.BACKEND:
        raise TrainingError("comparison inference requires the pinned local Ollama backend")
    local_ollama.literal_loopback_url(model["base_url"])
    for key in ("name", "digest", "ollama_version", "backend_version"):
        if not isinstance(model[key], str) or not model[key].strip():
            raise TrainingError("comparison model identity and installed versions must be pinned")
    for key, low, high in (
        ("seed", -(2**31), 2**31 - 1),
        ("num_predict", 1, 8192),
        ("repetitions", 1, 10),
    ):
        value = model[key]
        if type(value) is not int or not low <= value <= high:
            raise TrainingError(f"comparison {key} is outside the supported range")
    for key, low, high in (("temperature", 0, 2), ("timeout_seconds", 0, 600)):
        value = model[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not low <= value <= high
            or (key == "timeout_seconds" and value == 0)
        ):
            raise TrainingError(f"comparison {key} is outside the supported range")


def _validate_prepared(prepared: dict[str, Any]) -> None:
    if (
        prepared.get("purpose") != "private_training_comparison"
        or prepared.get("split") != "tuning"
        or prepared.get("private") is not True
        or prepared.get("router_attempt_limit") != 2
    ):
        raise TrainingError("comparison requires its private tuning-purpose attestation contract")
    if prepared.get("hashes", {}).get("implementation") != implementation_pins():
        raise TrainingError("comparison inference or scoring implementation changed")
    validate_model_settings(prepared["model"])
    if not isinstance(prepared.get("task"), str) or not prepared["task"]:
        raise TrainingError("comparison must contain one frozen task")
    if not isinstance(prepared.get("schema"), dict):
        raise TrainingError("comparison must contain its frozen output schema")
    if not all(isinstance(prepared.get(f"{side}_system"), str) for side in SIDES):
        raise TrainingError("comparison prompts must be frozen text")
    cases = prepared.get("cases")
    if not isinstance(cases, list) or not cases:
        raise TrainingError("comparison needs a nonempty frozen tuning cohort")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise TrainingError("comparison case identities must be unique")
    if prepared.get("target_case_id") not in ids:
        raise TrainingError("comparison must include the exact target case")
    for case in cases:
        if (
            not isinstance(case.get("messages"), list)
            or not isinstance(case.get("untrusted"), list)
            or not all(isinstance(value, str) for value in case["untrusted"])
            or not isinstance(case.get("expected"), dict)
            or not isinstance(case.get("checks"), list)
            or not case["checks"]
        ):
            raise TrainingError("comparison case evidence is incomplete")


def _cohorts() -> Any:
    from biz_kernel.training import cohorts

    return cohorts


def _prepare(session: Any, job_id: str) -> dict[str, Any]:
    prepared = _cohorts().prepare_execution(session, uuid.UUID(job_id))
    _validate_prepared(prepared)
    return prepared


def _verify(model: dict[str, Any]) -> dict[str, Any]:
    return local_ollama.verify_local_ollama(
        base_url=model["base_url"],
        model=model["name"],
        digest=model["digest"],
        ollama_version=model["ollama_version"],
        backend_version=model["backend_version"],
    )


@contextmanager
def _admission(job_id: str) -> Iterator[None]:
    # A duplicate process waits without turning a genuinely active call into an orphan.
    # A killed process releases this transaction lock; its durable start remains unknown.
    key = (
        int.from_bytes(hashlib.sha256(f"comparison-executor:{job_id}".encode()).digest()[:8], "big")
        >> 1
    )
    with db.session_scope() as session:
        if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
            raise ComparisonBusy("comparison is already running")
        control.admit(session, "resume before running a training comparison")
        yield


class _PinnedModel(OllamaModel):
    """Force the exact generation settings and persist every underlying Router attempt."""

    def __init__(self, model: dict[str, Any], job_id: str, attempt_key: str) -> None:
        super().__init__(
            model["base_url"],
            model["name"],
            temperature=model["temperature"],
            timeout=model["timeout_seconds"],
        )
        self.settings = model
        self.job_id, self.attempt_key, self.call_index = job_id, attempt_key, 0
        self._reported_usage: usage_mod.Usage | None = None

    async def structured(
        self, system: str, messages: list, schema: dict, tier: Any = "local"
    ) -> dict:
        self._reported_usage = None
        try:
            return await super().structured(system, messages, schema, tier)
        finally:
            # The generic adapter defaults omitted counters to zero. Comparison evidence
            # must distinguish an unreported counter from a measured zero, including when
            # response parsing or the post-call model check fails.
            usage_mod.take()
            if self._reported_usage is not None:
                usage_mod.report(self._reported_usage)

    def _observe(self, phase: str, observation: dict[str, Any]) -> None:
        with db.session_scope() as session:
            _cohorts().record_attempt_observation(
                session,
                uuid.UUID(self.job_id),
                attempt_key=self.attempt_key,
                call_index=self.call_index,
                phase=phase,
                observation=observation,
            )

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path != "/api/chat":
            raise TrainingError("comparison inference accepts only structured model drafts")
        _verify(self.settings)
        self.call_index += 1
        self._observe("started", {"usage": None, "api_cost_usd": None, "hardware_cost_usd": None})
        start = time.monotonic()
        observed: dict[str, Any] = {"usage": None, "api_cost_usd": None, "hardware_cost_usd": None}
        try:
            payload = {
                **body,
                "options": {
                    "temperature": self.settings["temperature"],
                    "seed": self.settings["seed"],
                    "num_predict": self.settings["num_predict"],
                },
            }
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=self.timeout
            ) as client:
                response = await client.post(self.base_url + path, json=payload)
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict):
                raise ValueError("model response is not an object")
            token_fields = ("prompt_eval_count", "eval_count")
            if all(type(result.get(key)) is int and result[key] >= 0 for key in token_fields):
                observed["usage"] = {
                    "input_tokens": result["prompt_eval_count"],
                    "output_tokens": result["eval_count"],
                }
                observed["api_cost_usd"] = "0"
                self._reported_usage = usage_mod.Usage(
                    input_tokens=result["prompt_eval_count"], output_tokens=result["eval_count"]
                )
            _verify(self.settings)
            return result
        except BaseException as exc:
            observed["error"] = type(exc).__name__
            raise
        finally:
            observed["latency_ms"] = (time.monotonic() - start) * 1000
            self._observe("finished", observed)


def _empty_result(
    side: str, case_id: str, repetition: int, error: str, *, unknown: bool = False
) -> dict[str, Any]:
    return {
        "side": side,
        "case_id": case_id,
        "repetition": repetition,
        "status": "unknown" if unknown else "error",
        "passed": False,
        "score": 0.0,
        "failures": [],
        "output": None,
        "error": error,
        "latency_ms": None,
        "usage": None,
        "api_cost_usd": None,
        "hardware_cost_usd": None,
        "attempts": None if unknown else 0,
        "unmetered_calls": None,
    }


@DBOS.step(name="biz.training.comparison.begin", retries_allowed=False)
def begin_job(job_id: str) -> dict[str, Any]:
    with db.session_scope() as session:
        if not _cohorts().start_job(session, uuid.UUID(job_id)):
            return {"already_finished": True}
        return _prepare(session, job_id)


@DBOS.step(name="biz.training.comparison.case", retries_allowed=False)
def compare_case(job_id: str, side: str, case_id: str, repetition: int) -> dict[str, Any]:
    with _admission(job_id):
        with db.session_scope() as session:
            prepared = _prepare(session, job_id)
            if side not in SIDES or not 1 <= repetition <= prepared["model"]["repetitions"]:
                raise TrainingError("comparison attempt is outside the frozen contract")
            case_data = next((case for case in prepared["cases"] if case["id"] == case_id), None)
            if case_data is None:
                raise TrainingError("comparison case is outside the frozen cohort")
            claim = _cohorts().begin_attempt(
                session, uuid.UUID(job_id), side=side, case_id=case_id, repetition=repetition
            )
        if claim["state"] == "completed":
            return claim["result"]
        if claim["state"] == "unknown":
            return _empty_result(
                side,
                case_id,
                repetition,
                "inference outcome is unknown after interruption",
                unknown=True,
            )
        if claim["state"] != "new":
            raise TrainingError("comparison attempt claim is invalid")
        result = _empty_result(side, case_id, repetition, "inference did not complete")
        routed = None
        start = time.monotonic()
        try:
            before = _verify(prepared["model"])
            model = _PinnedModel(prepared["model"], job_id, claim["attempt_key"])
            router = Router(local=model, cloud=None)
            case = harness.Case(
                name=case_id,
                system=prepared[f"{side}_system"],
                messages=case_data["messages"],
                schema=prepared["schema"],
                expected=case_data["expected"],
                checks=case_data["checks"],
                untrusted=case_data["untrusted"],
            )
            routed = asyncio.run(
                router.generate(
                    prepared["task"],
                    case.system,
                    case.messages,
                    case.schema,
                    tier_hint="local",
                    untrusted=case.untrusted,
                    escalate=False,
                )
            )
            scored = harness.score_case(case, routed.output)
            result.update(
                passed=scored.passed,
                score=scored.score,
                failures=scored.failures,
                output=scored.output,
                model_before=before,
            )
            after = _verify(prepared["model"])
            result.update(
                status="completed" if routed.attempts == 1 else "error",
                error=scored.error
                if routed.attempts == 1
                else "model output required a validation retry",
                model_after=after,
            )
        except (KeyboardInterrupt, SystemExit):
            # The start and any per-call observations have already committed. A subsequent
            # worker reports unknown; it never repeats this side/case automatically.
            raise
        except Exception as exc:
            routed = routed or spent_before(exc)
            result["error"] = (
                str(exc)
                if isinstance(exc, local_ollama.LocalModelError)
                else f"comparison inference failed ({type(exc).__name__})"
            )
        finally:
            result["latency_ms"] = (time.monotonic() - start) * 1000
        if routed is not None:
            result.update(
                usage={**asdict(routed.usage), "cost_usd": str(routed.cost_usd)},
                api_cost_usd=str(routed.cost_usd) if routed.unmetered_calls == 0 else None,
                attempts=routed.attempts,
                unmetered_calls=routed.unmetered_calls,
            )
        with db.session_scope() as session:
            _cohorts().finish_attempt(
                session, uuid.UUID(job_id), attempt_key=claim["attempt_key"], result=result
            )
        return result


def summarize(
    prepared: dict[str, Any], results: list[dict[str, Any]], *, error: str | None = None
) -> dict[str, Any]:
    """A single older-case regression blocks a useful comparison, regardless of totals."""
    expected = {
        (side, case["id"], rep)
        for side in SIDES
        for case in prepared["cases"]
        for rep in range(1, prepared["model"]["repetitions"] + 1)
    }
    keyed = {(row["side"], row["case_id"], row["repetition"]): row for row in results}
    missing = sorted(expected - keyed.keys())
    errors = [
        {
            "side": row["side"],
            "case_id": row["case_id"],
            "repetition": row["repetition"],
            "error": row.get("error") or "incomplete inference",
        }
        for row in results
        if row.get("status") != "completed" or row.get("error")
    ]
    regressions, target = [], []
    for case in prepared["cases"]:
        for rep in range(1, prepared["model"]["repetitions"] + 1):
            baseline, candidate = (keyed.get((side, case["id"], rep)) for side in SIDES)
            if baseline is None or candidate is None:
                continue
            pair = {
                "case_id": case["id"],
                "repetition": rep,
                "baseline_passed": baseline.get("passed") is True,
                "candidate_passed": candidate.get("passed") is True,
            }
            if pair["baseline_passed"] and not pair["candidate_passed"]:
                regressions.append(pair)
            if case["id"] == prepared["target_case_id"]:
                target.append(pair)
    valid = not (
        missing or errors or error or len(keyed) != len(results) or keyed.keys() - expected
    )
    baseline_passed = sum(row.get("passed") is True for row in results if row["side"] == "baseline")
    candidate_passed = sum(
        row.get("passed") is True for row in results if row["side"] == "candidate"
    )
    return {
        "valid": valid,
        "verdict": "invalid"
        if not valid
        else (
            "regression"
            if regressions
            else ("improved" if candidate_passed > baseline_passed else "no_improvement")
        ),
        "baseline_passed": baseline_passed,
        "candidate_passed": candidate_passed,
        "total_cases": len(prepared["cases"]) * prepared["model"]["repetitions"],
        "regressions": regressions,
        "target_outcomes": target,
        "errors": errors,
        "missing": [
            {"side": side, "case_id": key, "repetition": rep} for side, key, rep in missing
        ],
        "error": error,
        "results": results,
        "known_api_cost_usd": str(
            sum(
                (
                    Decimal(row["api_cost_usd"])
                    for row in results
                    if row.get("api_cost_usd") is not None
                ),
                Decimal(0),
            )
        ),
        "unmeasured_api_cost_results": sum(row.get("api_cost_usd") is None for row in results),
        "hardware_cost_usd": None,
        "electricity_cost_usd": None,
        "readiness_established": False,
        "activation_permitted": False,
    }


def _validate_results(
    prepared: dict[str, Any], results: list[dict[str, Any]], events: list[Any]
) -> None:
    """Bind DBOS checkpoint values to sealed application evidence and current scoring."""
    evidence = {event.event_key: event.payload for event in events}
    cases = {case["id"]: case for case in prepared["cases"]}
    identity = {
        key: prepared["model"][key]
        for key in ("backend", "backend_version", "base_url", "name", "digest", "ollama_version")
    }
    for result in results:
        key = f"{result['side']}:{result['case_id']}:{result['repetition']}"
        started = f"attempt:{key}:started"
        finished = f"attempt:{key}:finished"
        if result.get("status") == "unknown":
            if started not in evidence or finished in evidence:
                raise TrainingError("comparison unknown result disagrees with durable evidence")
            continue
        if evidence.get(finished) != result or started not in evidence:
            raise TrainingError("comparison cached result differs from durable evidence")
        if result.get("status") != "completed" or result.get("error"):
            continue
        case_data = cases[result["case_id"]]
        scored = harness.score_case(
            harness.Case(
                name=result["case_id"],
                system=prepared[f"{result['side']}_system"],
                messages=case_data["messages"],
                schema=prepared["schema"],
                expected=case_data["expected"],
                checks=case_data["checks"],
                untrusted=case_data["untrusted"],
            ),
            result["output"],
        )
        if (
            result.get("passed") != scored.passed
            or result.get("score") != scored.score
            or result.get("failures") != scored.failures
            or scored.error
            or result.get("model_before") != identity
            or result.get("model_after") != identity
        ):
            raise TrainingError("comparison result no longer matches its scorer and model pins")
        attempts = result.get("attempts")
        if type(attempts) is not int or not 1 <= attempts <= 2:
            raise TrainingError("comparison result has incomplete call evidence")
        usage_totals, unmetered = {"input_tokens": 0, "output_tokens": 0}, 0
        for index in range(1, attempts + 1):
            prefix = f"observation:{key}:{index}"
            if f"{prefix}:started" not in evidence or f"{prefix}:finished" not in evidence:
                raise TrainingError("comparison result has incomplete call evidence")
            observation = evidence[f"{prefix}:finished"]["observation"]
            if observation.get("error"):
                raise TrainingError("a failed model call invalidates the comparison")
            usage = observation.get("usage")
            if usage is None:
                unmetered += 1
            else:
                for field in usage_totals:
                    usage_totals[field] += usage[field]
        if (
            result.get("unmetered_calls") != unmetered
            or any(
                (result.get("usage") or {}).get(field) != value
                for field, value in usage_totals.items()
            )
            or result.get("api_cost_usd") != (None if unmetered else "0")
        ):
            raise TrainingError("comparison usage differs from its durable call evidence")


@DBOS.step(name="biz.training.comparison.finish", retries_allowed=False)
def finish_job(
    job_id: str, prepared: dict[str, Any], results: list[dict[str, Any]], error: str | None = None
) -> dict[str, Any]:
    # Reverify model even when all preceding case steps came from DBOS checkpoints.
    try:
        with db.session_scope() as session:
            current = _prepare(session, job_id)
            events = _cohorts().events(session, uuid.UUID(job_id))
            _validate_results(current, results, events)
        if current != prepared:
            raise TrainingError("comparison evidence changed while running")
        _verify(prepared["model"])
    except control.Paused:
        raise
    except Exception as exc:
        error = (
            str(exc)
            if isinstance(exc, local_ollama.LocalModelError)
            else "comparison evidence or implementation changed"
        )
    summary = summarize(prepared, results, error=error)
    with db.session_scope() as session:
        _cohorts().finish_job(session, uuid.UUID(job_id), summary=summary)
    return summary


@DBOS.step(name="biz.training.comparison.invalidate", retries_allowed=False)
def invalidate_job(job_id: str) -> dict[str, Any]:
    summary = {
        "valid": False,
        "verdict": "invalid",
        "error": "comparison authorization, evidence or implementation could not be verified",
        "results": [],
        "regressions": [],
        "errors": [],
        "missing": [],
        "readiness_established": False,
        "activation_permitted": False,
        "hardware_cost_usd": None,
        "electricity_cost_usd": None,
    }
    with db.session_scope() as session:
        _cohorts().finish_job(session, uuid.UUID(job_id), summary=summary)
    return summary


@DBOS.workflow(name="biz.training.comparison")
def comparison_workflow(job_id: str) -> dict[str, Any]:
    def wait_call(fn: Any, *args: Any) -> Any:
        while True:
            try:
                return fn(*args)
            except (control.Paused, ComparisonBusy):
                DBOS.sleep(1)

    try:
        prepared = wait_call(begin_job, job_id)
    except (TrainingError, local_ollama.LocalModelError):
        return invalidate_job(job_id)
    if prepared.get("already_finished"):
        return {"reused": True, "status": "finished"}
    results: list[dict[str, Any]] = []
    error = None
    for rep in range(1, prepared["model"]["repetitions"] + 1):
        for case in prepared["cases"]:
            for side in SIDES:
                try:
                    result = wait_call(compare_case, job_id, side, case["id"], rep)
                except (TrainingError, local_ollama.LocalModelError):
                    error = "comparison evidence or configuration could not be verified"
                    return wait_call(finish_job, job_id, prepared, results, error)
                results.append(result)
                if result.get("status") != "completed" or result.get("error"):
                    return wait_call(finish_job, job_id, prepared, results, error)
    return wait_call(finish_job, job_id, prepared, results, error)


class ComparisonHandle:
    """DBOS caches execution; application seals remain authoritative on every read."""

    def __init__(self, job_id: str, handle: Any) -> None:
        self.job_id, self.handle = job_id, handle

    def get_result(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        cached = self.handle.get_result(*args, **kwargs)
        with db.session_scope() as session:
            job = _cohorts().get_comparison(session, uuid.UUID(self.job_id))
            _, events, final = _cohorts().comparison_status(session, job)
            prepared = {"job_id": str(job.id), **job.contract}
        if final is None or (cached.get("reused") is not True and cached != final):
            raise TrainingError("cached comparison differs from durable final evidence")
        if final.get("valid") is True:
            _validate_prepared(prepared)
            _validate_results(prepared, final["results"], events)
            if summarize(prepared, final["results"], error=final.get("error")) != final:
                raise TrainingError("cached comparison no longer agrees with its scored evidence")
        return final

    def __getattr__(self, name: str) -> Any:
        return getattr(self.handle, name)


def start_comparison(job_id: uuid.UUID | str) -> Any:
    value = str(uuid.UUID(str(job_id)))
    with SetWorkflowID(f"training-comparison:{value}"):
        return ComparisonHandle(value, DBOS.start_workflow(comparison_workflow, value))
