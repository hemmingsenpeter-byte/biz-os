"""Private comparison calls preserve unknowns and run only the pinned local contract."""

from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx
from biz_kernel.models import local_ollama
from biz_kernel.models.router import Router, spent_before
from biz_kernel.training import comparison
from biz_kernel.training.service import TrainingError

SCHEMA = {
    "type": "object",
    "required": ["body"],
    "properties": {"body": {"type": "string"}},
    "additionalProperties": False,
}


def prepared() -> dict[str, Any]:
    return {
        "purpose": "private_training_comparison",
        "split": "tuning",
        "private": True,
        "router_attempt_limit": 2,
        "job_id": str(uuid.uuid4()),
        "task": "writer.compose",
        "schema": SCHEMA,
        "baseline_system": "Prepare a factual reply.",
        "candidate_system": "Ask for evidence.",
        "target_case_id": "target",
        "hashes": {"implementation": comparison.implementation_pins()},
        "model": {
            "backend": local_ollama.BACKEND,
            "name": "fixture:local",
            "digest": "a" * 64,
            "ollama_version": "0.fixture",
            "backend_version": local_ollama.adapter_version(),
            "base_url": "http://127.0.0.1:11434",
            "temperature": 0.1,
            "seed": 42,
            "num_predict": 128,
            "timeout_seconds": 10,
            "repetitions": 1,
        },
        "cases": [
            {
                "id": "target",
                "messages": [],
                "untrusted": ["private request"],
                "expected": {"body": "fixed"},
                "checks": [{"type": "json_schema"}, {"type": "exact"}],
            }
        ],
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://10.20.30.40:11434",
        "http://host.docker.internal:11434",
        "https://remote.invalid",
        "http://user:secret@127.0.0.1:11434",
        "http://127.0.0.1:11434/proxy",
        "http://127.0.0.1:11434?redirect=x",
    ],
)
def test_nonliteral_or_ambiguous_model_urls_refused_without_requests(monkeypatch, url):
    request = Mock(side_effect=AssertionError("invalid URLs must not make metadata requests"))
    monkeypatch.setattr(local_ollama.httpx, "Client", request)
    with pytest.raises(local_ollama.LocalModelError, match="literal loopback"):
        local_ollama.verify_local_ollama(base_url=url, model="fixture:local", digest="a" * 64)
    request.assert_not_called()


@respx.mock
def test_installed_model_verification_uses_exact_digest_versions_and_no_proxy(monkeypatch):
    endpoint = "http://127.0.0.1:11434"
    respx.get(endpoint + "/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "fixture:local", "digest": "a" * 64}]}
        )
    )
    respx.get(endpoint + "/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.fixture"})
    )
    factory = Mock(wraps=httpx.Client)
    monkeypatch.setattr(local_ollama.httpx, "Client", factory)
    identity = local_ollama.verify_local_ollama(
        base_url=endpoint,
        model="fixture:local",
        digest="a" * 64,
        ollama_version="0.fixture",
        backend_version=local_ollama.adapter_version(),
    )
    assert identity["backend"] == local_ollama.BACKEND
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["follow_redirects"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"digest": "b" * 64},
        {"remote_host": "https://remote.invalid"},
        {"remote_model": "cloud:model"},
    ],
)
@respx.mock
def test_model_drift_or_remote_metadata_blocks_before_inference(change):
    respx.get("http://127.0.0.1:11434/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "fixture:local", "digest": "a" * 64, **change}]}
        )
    )
    with pytest.raises(local_ollama.LocalModelError):
        local_ollama.verify_local_ollama(
            base_url="http://127.0.0.1:11434", model="fixture:local", digest="a" * 64
        )


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"prompt_eval_count": True, "eval_count": 2},
        {"prompt_eval_count": -1, "eval_count": 2},
        {"prompt_eval_count": 2, "eval_count": "3"},
    ],
)
def test_missing_or_invalid_usage_remains_unmetered(monkeypatch, counts):
    setup = prepared()
    observed, payloads = [], []
    monkeypatch.setattr(comparison, "_verify", lambda _: {"digest": "a" * 64})
    model = comparison._PinnedModel(setup["model"], setup["job_id"], "attempt")
    monkeypatch.setattr(model, "_observe", lambda phase, data: observed.append((phase, data)))
    real_client = httpx.AsyncClient

    def handle(request):
        import json

        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"body":"fixed"}'}, **counts})

    factory = Mock(
        side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    monkeypatch.setattr(comparison.httpx, "AsyncClient", factory)
    result = asyncio.run(
        Router(local=model).generate("writer.compose", "draft", [], SCHEMA, escalate=False)
    )
    assert result.unmetered_calls == 1
    assert observed[0][0] == "started" and observed[1][0] == "finished"
    assert observed[1][1]["usage"] is None and observed[1][1]["api_cost_usd"] is None
    assert payloads[0]["options"] == {"temperature": 0.1, "seed": 42, "num_predict": 128}
    assert factory.call_args.kwargs["trust_env"] is False


def test_usage_survives_invalid_model_json(monkeypatch):
    setup = prepared()
    monkeypatch.setattr(comparison, "_verify", lambda _: {"digest": "a" * 64})
    model = comparison._PinnedModel(setup["model"], setup["job_id"], "attempt")
    monkeypatch.setattr(model, "_observe", lambda *args: None)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comparison.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "message": {"content": "invalid JSON"},
                        "prompt_eval_count": 8,
                        "eval_count": 3,
                    },
                )
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ValueError) as caught:
        asyncio.run(
            Router(local=model).generate("writer.compose", "draft", [], SCHEMA, escalate=False)
        )
    spent = spent_before(caught.value)
    assert spent is not None
    assert spent.usage.input_tokens == 8 and spent.usage.output_tokens == 3
    assert spent.unmetered_calls == 0


def test_configuration_and_implementation_changes_fail_closed():
    setup = prepared()
    comparison._validate_prepared(setup)
    changed = copy.deepcopy(setup)
    changed["hashes"]["implementation"]["router_sha256"] = "b" * 64
    with pytest.raises(TrainingError, match="implementation changed"):
        comparison._validate_prepared(changed)
    changed = copy.deepcopy(setup)
    changed["model"]["seed"] = True
    with pytest.raises(TrainingError, match="seed"):
        comparison._validate_prepared(changed)


def row(side: str, case_id: str, passed: bool, **extra: Any) -> dict[str, Any]:
    return {
        "side": side,
        "case_id": case_id,
        "repetition": 1,
        "status": "completed",
        "passed": passed,
        "error": None,
        "api_cost_usd": "0",
        **extra,
    }


def test_one_older_regression_blocks_comparison_despite_better_total():
    setup = prepared()
    setup["cases"] += [
        {**setup["cases"][0], "id": "older"},
        {**setup["cases"][0], "id": "improved"},
    ]
    results = [
        row("baseline", "target", False),
        row("candidate", "target", True),
        row("baseline", "older", True),
        row("candidate", "older", False),
        row("baseline", "improved", False),
        row("candidate", "improved", True),
    ]
    summary = comparison.summarize(setup, results)
    assert summary["candidate_passed"] > summary["baseline_passed"]
    assert summary["valid"] is True and summary["verdict"] == "regression"
    assert summary["regressions"][0]["case_id"] == "older"
    assert summary["activation_permitted"] is False and summary["hardware_cost_usd"] is None


def test_missing_and_unknown_results_invalidate_comparison():
    setup = prepared()
    summary = comparison.summarize(setup, [row("baseline", "target", True)])
    assert summary["valid"] is False and summary["missing"]
    summary = comparison.summarize(
        setup,
        [
            row("baseline", "target", True),
            row("candidate", "target", False, status="unknown", api_cost_usd=None),
        ],
    )
    assert summary["valid"] is False and summary["unmeasured_api_cost_results"] == 1


@pytest.fixture
def committed_comparison(fresh_db):  # type: ignore[no-untyped-def]
    from biz_kernel import db
    from biz_kernel.models_registry import import_all_models
    from biz_kernel.training import cohorts, service

    from .conftest import EXPECTED, source

    old_engine, old_factory = db._engine, db._factory
    engine = db.configure(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(engine)
    with db.session_scope() as session:
        world = source(session)
        candidate = service.create_candidate(
            session,
            world["correction"].id,
            rule="Ask for evidence.",
            by="owner",
            snapshot_id=world["snapshot"].id,
            expected_output=EXPECTED,
        )
        version = service.create_version(
            session,
            candidate.id,
            parent_id=None,
            rule="Ask for evidence.",
            system="Ask before replying.",
            expected_output=EXPECTED,
            by="owner",
        )
        service.decide_version(session, version.id, decision="accepted_for_testing", by="owner")
        cohort = cohorts.create_cohort(
            session, name="private tuning", version_ids=[version.id], by="owner"
        )
        cohorts.attest_cohort(session, cohort.id, by="owner")
        model = prepared()["model"]
        del model["backend"]
        job = cohorts.enqueue(
            session,
            version_id=version.id,
            cohort_id=cohort.id,
            model=model,
            idempotency_key="comparison",
            by="owner",
        )
        again = cohorts.enqueue(
            session,
            version_id=version.id,
            cohort_id=cohort.id,
            model=model,
            idempotency_key="comparison",
            by="owner",
        )
        assert again.id == job.id
        job_id = str(job.id)
    try:
        yield fresh_db, job_id
    finally:
        engine.dispose()
        db._engine, db._factory = old_engine, old_factory


def _fake_transport(monkeypatch, setup, *, error=None):  # type: ignore[no-untyped-def]
    import json

    seen = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comparison,
        "_verify",
        lambda model: {
            key: model[key]
            for key in (
                "backend",
                "backend_version",
                "base_url",
                "name",
                "digest",
                "ollama_version",
            )
        },
    )

    def handler(request):
        seen.append(json.loads(request.content))
        if error:
            raise error
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(setup["cases"][0]["expected"])},
                "prompt_eval_count": 5,
                "eval_count": 2,
            },
        )

    monkeypatch.setattr(
        comparison.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


def test_completed_case_is_reused_from_durable_application_evidence(
    committed_comparison, monkeypatch
):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    seen = _fake_transport(monkeypatch, setup)
    call = inspect.unwrap(comparison.compare_case)
    result = call(job_id, "baseline", setup["target_case_id"], 1)
    repeated = call(job_id, "baseline", setup["target_case_id"], 1)
    assert result == repeated and result["passed"] is True
    assert len(seen) == 1
    with db.session_scope() as session:
        events = cohorts.events(session, uuid.UUID(job_id))
        assert sum(event.kind == "attempt_started" for event in events) == 1
        assert sum(event.kind == "attempt_finished" for event in events) == 1
        assert sum(event.kind == "model_observation" for event in events) == 2


def test_orphaned_started_attempt_stays_unknown_without_new_inference(
    committed_comparison, monkeypatch
):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
        cohorts.begin_attempt(
            session,
            uuid.UUID(job_id),
            side="baseline",
            case_id=setup["target_case_id"],
            repetition=1,
        )
    seen = _fake_transport(monkeypatch, setup)
    result = inspect.unwrap(comparison.compare_case)(job_id, "baseline", setup["target_case_id"], 1)
    assert result["status"] == "unknown" and result["api_cost_usd"] is None
    assert seen == []
    summary = inspect.unwrap(comparison.finish_job)(job_id, setup, [result])
    assert summary["valid"] is False and summary["missing"]
    with db.session_scope() as session:
        assert uuid.UUID(job_id) not in cohorts.pending_comparisons(session)


def test_paused_executor_starts_no_model_attempt(committed_comparison, monkeypatch):
    import inspect

    from biz_kernel import db
    from biz_kernel.control import service as control
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
        control.pause(session, "owner")
    seen = _fake_transport(monkeypatch, setup)
    with pytest.raises(control.Paused):
        inspect.unwrap(comparison.compare_case)(job_id, "baseline", setup["target_case_id"], 1)
    assert seen == []
    with db.session_scope() as session:
        assert not cohorts.events(session, uuid.UUID(job_id))


def test_timeout_keeps_attempt_observation_and_unknown_usage(committed_comparison, monkeypatch):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    seen = _fake_transport(
        monkeypatch, setup, error=httpx.ReadTimeout("private payload must stay out of diagnostics")
    )
    result = inspect.unwrap(comparison.compare_case)(job_id, "baseline", setup["target_case_id"], 1)
    assert result["status"] == "error" and result["api_cost_usd"] is None
    assert result["attempts"] == 1 and result["unmetered_calls"] == 1
    assert "private payload" not in result["error"]
    assert len(seen) == 1
    with db.session_scope() as session:
        observations = [
            event.payload
            for event in cohorts.events(session, uuid.UUID(job_id))
            if event.kind == "model_observation"
        ]
        assert observations[-1]["observation"]["usage"] is None
        assert observations[-1]["observation"]["latency_ms"] >= 0


def test_drift_after_inference_keeps_usage_and_invalidates_result(
    committed_comparison, monkeypatch
):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    seen = _fake_transport(monkeypatch, setup)
    checks = []

    def verify(model):
        checks.append(True)
        if len(checks) >= 4:
            raise local_ollama.LocalModelError("model digest does not match the local installation")
        return {"digest": model["digest"]}

    monkeypatch.setattr(comparison, "_verify", verify)
    result = inspect.unwrap(comparison.compare_case)(job_id, "baseline", setup["target_case_id"], 1)
    assert result["status"] == "error" and result["usage"]["input_tokens"] == 5
    assert result["output"] == setup["cases"][0]["expected"]
    assert len(seen) == 1
    assert comparison.summarize(setup, [result])["valid"] is False


def test_dbos_crash_replay_reuses_baseline_and_does_not_repeat_unknown_candidate(
    committed_comparison, tmp_path
):
    import subprocess
    import sys
    from pathlib import Path

    from biz_kernel import db
    from biz_kernel.training import cohorts
    from sqlalchemy.engine import make_url

    url, job_id = committed_comparison
    worker = Path(__file__).with_name("comparison_worker.py")
    parsed = make_url(url)
    system_url = parsed.set(database=f"{parsed.database}_comparison").render_as_string(
        hide_password=False
    )
    marker = tmp_path / "interrupted"

    def run(mode):
        return subprocess.run(
            [sys.executable, str(worker), url, system_url, job_id, mode, str(marker)],
            text=True,
            capture_output=True,
            timeout=90,
        )

    first = run("crash")
    assert first.returncode == 17, first.stdout + first.stderr[-4000:]
    assert marker.exists()
    resumed = run("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr[-4000:]
    assert '"valid": false' in resumed.stdout
    assert marker.with_suffix(".calls").read_text().splitlines() == ["baseline", "candidate"]
    replay = run("replay")
    assert replay.returncode == 0, replay.stdout + replay.stderr[-4000:]
    assert marker.with_suffix(".calls").read_text().splitlines() == ["baseline", "candidate"]
    with db.session_scope() as session:
        status, events, final = cohorts.comparison_status(
            session, cohorts.get_comparison(session, uuid.UUID(job_id))
        )
        assert status in {"failed", "unknown"} and final["valid"] is False
        assert sum(event.kind == "attempt_started" for event in events) == 2
        assert sum(event.kind == "attempt_finished" for event in events) == 1


def test_finalizer_rejects_cached_result_different_from_durable_evidence(
    committed_comparison, monkeypatch
):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    _fake_transport(monkeypatch, setup)
    call = inspect.unwrap(comparison.compare_case)
    results = [call(job_id, side, setup["target_case_id"], 1) for side in comparison.SIDES]
    results[0] = {**results[0], "passed": False, "score": 0.0}
    summary = inspect.unwrap(comparison.finish_job)(job_id, setup, results)
    assert summary["valid"] is False and "evidence" in summary["error"]


def test_finalizer_rechecks_tampered_attempt_event_seals(committed_comparison, monkeypatch):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts
    from sqlalchemy import text

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    _fake_transport(monkeypatch, setup)
    call = inspect.unwrap(comparison.compare_case)
    results = [call(job_id, side, setup["target_case_id"], 1) for side in comparison.SIDES]
    with db.engine().begin() as connection:
        connection.execute(text("ALTER TABLE training_comparison_events DISABLE TRIGGER USER"))
        connection.execute(
            text(
                "UPDATE training_comparison_events "
                "SET payload = payload || '{\"score\": 0}'::jsonb "
                "WHERE job_id = :job AND kind = 'attempt_finished'"
            ),
            {"job": uuid.UUID(job_id)},
        )
        connection.execute(text("ALTER TABLE training_comparison_events ENABLE TRIGGER USER"))
    summary = inspect.unwrap(comparison.finish_job)(job_id, setup, results)
    assert summary["valid"] is False and summary["verdict"] == "invalid"


def test_dbos_successful_pair_and_duplicate_run_reuse_all_calls(committed_comparison, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from biz_kernel import db
    from biz_kernel.training import cohorts
    from sqlalchemy.engine import make_url

    url, job_id = committed_comparison
    worker = Path(__file__).with_name("comparison_worker.py")
    parsed = make_url(url)
    system_url = parsed.set(database=f"{parsed.database}_comparison").render_as_string(
        hide_password=False
    )
    marker = tmp_path / "successful"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(worker), url, system_url, job_id, "success", str(marker)],
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr[-4000:]
        assert '"valid": true' in result.stdout and '"verdict": "improved"' in result.stdout
    assert marker.with_suffix(".calls").read_text().splitlines() == ["baseline", "candidate"]
    with db.session_scope() as session:
        status, _, final = cohorts.comparison_status(
            session, cohorts.get_comparison(session, uuid.UUID(job_id))
        )
        assert status == "completed" and final["valid"] is True


def test_cli_prints_only_aggregate_private_results(monkeypatch):
    from biz_kernel.training import cli
    from typer.testing import CliRunner

    monkeypatch.setattr(cli, "_launch", lambda *args: None)
    monkeypatch.setattr(cli.DBOS, "destroy", lambda **kwargs: None)
    handle = Mock()
    handle.get_result.return_value = {
        "valid": True,
        "verdict": "improved",
        "baseline_passed": 0,
        "candidate_passed": 1,
        "total_cases": 1,
        "results": [{"output": {"body": "PRIVATE_TEXT"}}],
    }
    monkeypatch.setattr(comparison, "start_comparison", lambda job: handle)
    result = CliRunner().invoke(cli.app, ["run-comparison", str(uuid.uuid4())])
    assert result.exit_code == 0, result.output
    assert "PRIVATE_TEXT" not in result.output and '"candidate_passed": 1' in result.output


def test_completed_dbos_cache_cannot_bypass_current_attempt_event_seals(
    committed_comparison, monkeypatch
):
    import inspect

    from biz_kernel import db
    from biz_kernel.training import cohorts
    from sqlalchemy import text

    _, job_id = committed_comparison
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    seen = _fake_transport(monkeypatch, setup)
    call = inspect.unwrap(comparison.compare_case)
    results = [call(job_id, side, setup["target_case_id"], 1) for side in comparison.SIDES]
    final = inspect.unwrap(comparison.finish_job)(job_id, setup, results)
    assert final["valid"] is True
    cached = Mock()
    cached.get_result.return_value = final
    handle = comparison.ComparisonHandle(job_id, cached)
    assert handle.get_result()["valid"] is True
    with db.engine().begin() as connection:
        connection.execute(text("ALTER TABLE training_comparison_events DISABLE TRIGGER USER"))
        connection.execute(
            text(
                "UPDATE training_comparison_events "
                "SET payload = payload || '{\"score\": 0}'::jsonb "
                "WHERE job_id = :job AND kind = 'attempt_finished'"
            ),
            {"job": uuid.UUID(job_id)},
        )
        connection.execute(text("ALTER TABLE training_comparison_events ENABLE TRIGGER USER"))
    with pytest.raises(TrainingError, match="integrity"):
        handle.get_result()
    assert len(seen) == 2
