"""Exercise the offline deployment checks with disposable settings."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deploy import preflight


@pytest.fixture
def compose_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], dict[str, Any]]:
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path / 'no-daemon.sock'}")
    try:
        subprocess.run(["docker", "compose", "version"], capture_output=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker Compose v2 is required to parse a disposable config")

    home = tmp_path / "objects"
    home.mkdir(mode=0o700)

    def resolve(composition: str) -> dict[str, Any]:
        environment = {
            "POSTGRES_PASSWORD": "disposable-postgres",
            "BIZ_SECRET_KEY": "disposable-app-secret",
            "BIZ_RP_ID": "localhost",
            "BIZ_ORIGIN": "http://localhost:3000",
            "BIZ_APP_DIR": f"/app/apps/{composition}",
            "BIZ_EXECUTION_MODE": "",
            "BIZ_STORAGE_HOST_ROOT": str(home),
            "DOCUSEAL_SECRET_KEY_BASE": "disposable-docuseal-secret",
        }
        if composition == "hive":
            environment.update(
                BIZ_RP_ID="biz.example.invalid",
                BIZ_ORIGIN="https://biz.example.invalid",
                BIZ_M365_TENANT="fixture-tenant",
                BIZ_M365_CLIENT_ID="fixture-ms-client",
                BIZ_M365_SECRET="fixture-ms-secret",
                BIZ_M365_MAILBOX="hello@example.invalid",
                BIZ_M365_TZ="Pacific/Auckland",
                BIZ_TWILIO_SID="fixture-twilio-sid",
                BIZ_TWILIO_TOKEN="fixture-twilio-token",
                BIZ_TWILIO_FROM="+15555555555",
                BIZ_TWILIO_WEBHOOK_URL="https://hooks.example.invalid/webhooks/telephony",
                BIZ_DOCUSEAL_API_KEY="fixture-docuseal-key",
                BIZ_DOCUSEAL_WEBHOOK_SECRET="fixture-docuseal-webhook-secret",
                BIZ_XERO_CLIENT_ID="fixture-xero-client",
                BIZ_XERO_CLIENT_SECRET="fixture-xero-secret",
                ANTHROPIC_API_KEY="fixture-anthropic-key",
            )
        for key in environment:
            monkeypatch.delenv(key, raising=False)
        env_file = tmp_path / f"{composition}.env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
        return preflight.resolve_compose(preflight.ROOT / "deploy/docker-compose.yml", env_file)

    return resolve


@pytest.mark.parametrize("composition", ["demo", "hive"])
def test_preflight_accepts_resolved_compose_without_provider_calls(
    compose_config: Callable[[str], dict[str, Any]], composition: str
) -> None:
    assert preflight.validate(compose_config(composition), preflight.ROOT) == []


def test_hive_reports_unwired_and_missing_provider_config(
    compose_config: Callable[[str], dict[str, Any]],
) -> None:
    config = compose_config("hive")
    api, worker = (config["services"][name]["environment"] for name in ("biz-api", "biz-worker"))
    del worker["BIZ_TWILIO_TOKEN"]
    for environment in (api, worker):
        environment["BIZ_M365_SECRET"] = "change-me"
        environment["BIZ_M365_TZ"] = "Invalid/Timezone"
    worker["BIZ_OLLAMA_MODEL"] = "different-model"
    assert preflight.validate(config, preflight.ROOT) == [
        "BIZ_M365_SECRET must be set for the selected composition",
        "BIZ_OLLAMA_MODEL must match between biz-api and biz-worker",
        "BIZ_TWILIO_TOKEN must be mapped for both biz-api and biz-worker",
        "BIZ_M365_TZ must be a valid IANA time zone",
    ]


def test_preflight_rejects_storage_and_route_exposure(
    compose_config: Callable[[str], dict[str, Any]],
) -> None:
    config = compose_config("hive")
    services = config["services"]
    storage = services["biz-api"]["volumes"][0]
    Path(storage["source"]).chmod(0o755)
    services["biz-web"]["volumes"] = [storage]
    services["caddy"]["ports"][0]["host_ip"] = "0.0.0.0"
    services["cloudflared"]["networks"]["default"] = {}
    problems = preflight.validate(config, preflight.ROOT)
    assert "Only biz-api and biz-worker may mount the object storage directory" in problems
    assert "BIZ_STORAGE_HOST_ROOT must be a directory with mode 0700" in problems
    assert "caddy may publish host ports only on loopback" in problems
    assert "cloudflared must use its isolated network and opt-in public profile" in problems


def test_preflight_reports_compose_error_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed_compose(*args: object) -> dict[str, Any]:
        raise subprocess.CalledProcessError(1, ["docker"], stderr="secret=do-not-include")

    monkeypatch.setattr(preflight, "resolve_compose", failed_compose)
    assert preflight.main([]) == 1
    result = capsys.readouterr()
    assert "Could not read and validate the local Compose configuration" in result.err
    assert "do-not-include" not in result.err + result.out


def test_preflight_requires_agreement_on_execution_mode(
    compose_config: Callable[[str], dict[str, Any]],
) -> None:
    config = compose_config("hive")
    services = config["services"]
    services["biz-api"]["environment"]["BIZ_EXECUTION_MODE"] = "unsupported-fixture"
    services["biz-worker"]["environment"]["BIZ_EXECUTION_MODE"] = "supervised"
    assert preflight.validate(config, preflight.ROOT) == [
        "BIZ_EXECUTION_MODE must match between biz-api and biz-worker",
        "BIZ_EXECUTION_MODE must be shadow or supervised",
    ]


def test_shadow_preflight_requires_the_appliance_model(
    compose_config: Callable[[str], dict[str, Any]],
) -> None:
    config = compose_config("hive")
    for name in ("biz-api", "biz-worker"):
        config["services"][name]["environment"].update(
            BIZ_OLLAMA_URL="https://remote.example.invalid",
            BIZ_OLLAMA_MODEL="fixture:cloud",
        )
    assert preflight.validate(config, preflight.ROOT) == [
        "Shadow Ollama must use the appliance host.docker.internal endpoint",
        "BIZ_OLLAMA_MODEL cannot use a cloud model in shadow mode",
    ]


def test_preflight_requires_the_database_and_local_path_captured_by_backups(
    compose_config: Callable[[str], dict[str, Any]],
) -> None:
    config = compose_config("hive")
    services = config["services"]
    services["biz-worker"]["environment"]["BIZ_DBOS_SYSTEM_URL"] = "postgresql://elsewhere/dbos"
    for service in ("biz-api", "biz-worker"):
        services[service]["volumes"][0]["source"] = "../var/objects"
    problems = preflight.validate(config, preflight.ROOT)
    assert "BIZ_STORAGE_HOST_ROOT must be one shared, absolute host directory" in problems
    assert "BIZ_DBOS_SYSTEM_URL must be empty so backups include the workflow database" in problems
