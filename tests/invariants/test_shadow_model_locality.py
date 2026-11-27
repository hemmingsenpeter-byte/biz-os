"""Shadow model configuration is checked before inference, including model overrides."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
import yaml
from biz_kernel.cli.main import doctor_cmd
from biz_kernel.runtime import CompositionError, build_runtime
from biz_kernel.tools.shadow import ExecutionMode, mode_problems
from biz_port_model_claude import ClaudeModel
from biz_port_model_ollama import OllamaModel

ROOT = Path(__file__).resolve().parents[2]
SLOTS = {slot: object() for slot in ("mail", "calendar", "telephony", "model_local")}


@pytest.fixture(autouse=True)
def clean_model_settings(monkeypatch):  # type: ignore[no-untyped-def]
    for key in (
        "BIZ_EXECUTION_MODE",
        "BIZ_OLLAMA_URL",
        "BIZ_OLLAMA_MODEL",
        "BIZ_OLLAMA_EMBED_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("BIZ_OLLAMA_MODEL", "gpt-oss:120b-cloud", "BIZ_OLLAMA_MODEL cannot use a cloud model"),
        (
            "BIZ_OLLAMA_EMBED_MODEL",
            "embed:cloud",
            "BIZ_OLLAMA_EMBED_MODEL cannot use a cloud model",
        ),
        ("BIZ_OLLAMA_URL", "https://remote.example.invalid", "must use a loopback"),
        ("BIZ_OLLAMA_URL", "http://user:secret@localhost:11434", "must use a loopback"),
        ("BIZ_OLLAMA_MODEL", "", "must name an installed local model"),
    ],
)
def test_runtime_and_doctor_refuse_before_any_port_is_built(
    monkeypatch, capsys, key, value, problem
):
    monkeypatch.setenv(key, value)
    resolve = Mock(side_effect=AssertionError("no adapter may be built on refused settings"))
    monkeypatch.setattr("biz_kernel.runtime.resolve_port", resolve)
    with pytest.raises(CompositionError, match=problem):
        build_runtime(ROOT / "apps/hive", ROOT)
    resolve.assert_not_called()
    with pytest.raises(typer.Exit):
        doctor_cmd(ROOT / "apps/hive", ROOT)
    output = capsys.readouterr().out
    assert problem in output
    assert "secret" not in output


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
        "http://host.docker.internal:11434",
    ],
)
def test_runtime_and_doctor_accept_local_generation_and_embedding(monkeypatch, capsys, url):
    monkeypatch.setenv("BIZ_OLLAMA_URL", url)
    monkeypatch.setenv("BIZ_OLLAMA_MODEL", "llama3.1:8b")
    monkeypatch.setenv("BIZ_OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")
    rt = build_runtime(ROOT / "apps/hive", ROOT, ports=SLOTS)
    assert rt.registry.execution_mode == ExecutionMode.SHADOW
    doctor_cmd(ROOT / "apps/hive", ROOT)
    assert "✓ execution mode shadow" in capsys.readouterr().out


@pytest.mark.parametrize(
    "model",
    [
        OllamaModel(model="gpt-oss:120b-cloud"),
        OllamaModel(embed_model="embedding:cloud"),
        OllamaModel(base_url="https://remote.example.invalid"),
    ],
)
def test_resolved_override_cannot_bypass_environment_check(monkeypatch, model):
    embedder = Mock(side_effect=AssertionError("must refuse before embedding setup"))
    monkeypatch.setattr("biz_kernel.runtime._embedder", embedder)
    with pytest.raises(CompositionError, match="cloud model|must use a loopback"):
        build_runtime(ROOT / "apps/hive", ROOT, ports={**SLOTS, "model_local": model})
    embedder.assert_not_called()


def test_resolved_ollama_subclass_is_checked(monkeypatch):
    class CustomModel(OllamaModel):
        pass

    with pytest.raises(CompositionError, match="BIZ_OLLAMA_EMBED_MODEL cannot use a cloud model"):
        build_runtime(
            ROOT / "apps/hive",
            ROOT,
            ports={**SLOTS, "model_local": CustomModel(embed_model="embedding:cloud")},
        )


def test_existing_cloud_opt_in_is_preserved_but_compose_remains_local(tmp_path, monkeypatch):
    target = Path(shutil.copytree(ROOT / "apps/hive", tmp_path / "hive"))
    policy_file = target / "policy.yaml"
    policy = yaml.safe_load(policy_file.read_text())
    policy["shadow"] = {"allow_cloud_model": True}
    policy_file.write_text(yaml.safe_dump(policy))
    env = {
        "BIZ_OLLAMA_URL": "https://remote.example.invalid",
        "BIZ_OLLAMA_MODEL": "model:cloud",
        "BIZ_OLLAMA_EMBED_MODEL": "embedding:cloud",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert build_runtime(target, ROOT, ports=SLOTS).registry.execution_mode == ExecutionMode.SHADOW
    doctor_cmd(target, ROOT)
    _, problems = mode_problems(
        "shadow",
        None,
        override=None,
        ports={"model_local": "model_ollama"},
        policy=policy,
        environ=env,
        compose=True,
    )
    assert len(problems) == 3
    assert "host.docker.internal" in problems[0]


def test_explicit_environment_is_respected(monkeypatch):
    monkeypatch.setenv("BIZ_OLLAMA_MODEL", "model:cloud")
    _, problems = mode_problems(
        "shadow",
        None,
        override=None,
        ports={"model_local": "model_ollama"},
        policy={},
        environ={},
    )
    assert problems == []


def test_resolved_cloud_adapter_in_local_slot_cannot_bypass_shadow(monkeypatch):
    cloud = ClaudeModel(client=Mock())
    embedder = Mock(side_effect=AssertionError("must refuse before embedding setup"))
    monkeypatch.setattr("biz_kernel.runtime._embedder", embedder)
    with pytest.raises(CompositionError, match="port model_local is a cloud model"):
        build_runtime(ROOT / "apps/hive", ROOT, ports={**SLOTS, "model_local": cloud})
    embedder.assert_not_called()
    _, problems = mode_problems(
        "shadow",
        None,
        override=None,
        ports={"model_local": cloud},
        policy={"shadow": {"allow_cloud_model": True}},
        environ={},
    )
    assert problems == []
