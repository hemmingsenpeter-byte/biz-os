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
) -> Callable[..., dict[str, Any]]:
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path / 'no-daemon.sock'}")
    try:
        subprocess.run(["docker", "compose", "version"], capture_output=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker Compose v2 is required to parse a disposable config")

    home = tmp_path / "objects"
    home.mkdir(mode=0o700)

    def resolve(composition: str, **extra: str) -> dict[str, Any]:
        environment = {
            "DOCUSEAL_HOST": "",
            "BIZ_TRUSTED_PROXIES": "",
            "BIZ_APP_SUBNET": "",
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
        environment.update(extra)
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


@pytest.mark.parametrize("mode", ["service:cloudflared", "service:tunnel-ingress", "container:x"])
def test_preflight_rejects_joining_another_network_namespace(
    compose_config: Callable[[str], dict[str, Any]], mode: str
) -> None:
    config = compose_config("hive")
    web = config["services"]["biz-web"]
    web.pop("networks", None)
    web.pop("ports", None)
    web["network_mode"] = mode
    problems = preflight.validate(config, preflight.ROOT)
    assert f"biz-web must not join another network namespace ({mode})" in problems


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
    needs_policy = "; shadow mode needs policy shadow.allow_cloud_model: true"
    assert preflight.validate(config, preflight.ROOT) == [
        # the runtime rule, shared with biz doctor and the API/worker start
        "port model_local sends prompts to a host off the appliance" + needs_policy,
        "port model_local names a cloud-hosted model" + needs_policy,
        # the appliance's stricter deploy rule
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


def test_preflight_refuses_an_override_that_loosens_shadow(
    compose_config: Callable[[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = compose_config("hive")
    for name in ("biz-api", "biz-worker"):
        config["services"][name]["environment"]["BIZ_EXECUTION_MODE"] = "supervised"
    problems = preflight.validate(config, preflight.ROOT)
    assert len(problems) == 1 and "cannot loosen the composition's shadow mode" in problems[0]

    # The operator's own shell does not leak into the composition check.
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "supervised")
    assert preflight.validate(compose_config("hive"), preflight.ROOT) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"ports": {"model_cloud": "model_claude"}}, "port model_cloud is a cloud model"),
        ({"execution_mode": "supervised"}, "supervised mode needs supervised_authorization"),
    ],
)
def test_preflight_names_execution_mode_problems(
    compose_config: Callable[[str], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
    problem: str,
) -> None:
    import dataclasses

    from biz_kernel.loader import load_composition

    def changed(app_dir: Path, root: Path) -> Any:
        manifest = load_composition(app_dir, root)
        ports = {**manifest.ports, **change.get("ports", {})}
        mode = change.get("execution_mode", manifest.execution_mode)
        return dataclasses.replace(manifest, ports=ports, execution_mode=mode)

    config = compose_config("hive")
    monkeypatch.setattr(preflight, "load_composition", changed)
    problems = preflight.validate(config, preflight.ROOT)
    assert any(problem in p for p in problems), problems


SIGNING_FORM_PATHS = {
    "/s/*",
    "/d/*",
    "/p/*",
    "/submit_form_email_2fa",
    "/start_form_email_2fa_send",
    "/resubmit_form",
}
SIGNING_ASSET_PATHS = {
    "/packs/*",
    "/preview/*",
    "/blobs_proxy/*",
    "/fonts/DancingScript-Regular.otf",
    "/favicon.ico",
    "/favicon.svg",
    "/favicon-16x16.png",
    "/favicon-32x32.png",
    "/favicon-96x96.png",
    "/apple-icon-180x180.png",
}
LINKS_OFF_HOST = (
    "DocuSeal signing links must use https://DOCUSEAL_HOST (set docuseal APP_URL from it)"
)
EMPTY_APP_URL = "DocuSeal APP_URL must be a non-empty HTTP or HTTPS origin"
NOT_PUBLIC = "DOCUSEAL_HOST must be the public signing hostname for DocuSeal signing"


def _matcher(caddyfile: str, name: str) -> dict[str, list[str]]:
    block = caddyfile.split(f"@{name} {{\n", 1)[1].split("\n\t}", 1)[0]
    lines = (line.split() for line in block.strip().splitlines())
    return {words[0]: words[1:] for words in lines if words}


@pytest.fixture
def signing_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load HIVE as if its composition also bound the DocuSeal signing port."""
    load = preflight.load_composition

    def with_signing(app_dir: Path, root: Path) -> Any:
        manifest = load(app_dir, root)
        manifest.ports["sign"] = "sign_docuseal"
        return manifest

    monkeypatch.setattr(preflight, "load_composition", with_signing)


def test_public_gateway_routes_only_signer_paths_on_the_signing_host() -> None:
    caddyfile = (preflight.ROOT / "deploy/PublicCaddyfile").read_text()
    form, asset = _matcher(caddyfile, "signing_form"), _matcher(caddyfile, "signing_asset")
    for matcher in (form, asset):
        assert matcher["host"] == ["{$DOCUSEAL_HOST:signing-disabled.invalid}"]
    assert set(form["path"]) == SIGNING_FORM_PATHS
    assert set(asset["path"]) == SIGNING_ASSET_PATHS
    assert asset["method"] == ["GET", "HEAD"]
    routed = " ".join(form["path"] + asset["path"])
    for private in ("/api", "/sign_in", "/setup", "/settings", "/templates", "/users", "/up"):
        assert private not in routed
    # Every DocuSeal upstream sits behind one of the two host-bound matchers.
    disk = _matcher(caddyfile, "signing_disk")
    assert disk["host"] == form["host"]
    assert disk["path"] == ["/disk/*"]
    assert disk["method"] == ["GET", "HEAD"]
    assert caddyfile.count("reverse_proxy docuseal:3000") == 3
    assert preflight.public_caddyfile_problems(caddyfile) == []


def test_compose_disables_signing_routes_unless_a_host_is_set(
    compose_config: Callable[..., dict[str, Any]],
) -> None:
    services = compose_config("demo")["services"]
    assert services["tunnel-ingress"]["environment"]["DOCUSEAL_HOST"] == "signing-disabled.invalid"
    assert services["docuseal"]["environment"]["APP_URL"] == "http://127.0.0.1:3002"

    services = compose_config("demo", DOCUSEAL_HOST="sign.example.com")["services"]
    assert services["tunnel-ingress"]["environment"]["DOCUSEAL_HOST"] == "sign.example.com"
    assert services["docuseal"]["environment"]["APP_URL"] == "https://sign.example.com"


@pytest.mark.parametrize("composition", ["demo", "hive"])
def test_preflight_flags_a_signing_host_without_a_signing_port(
    compose_config: Callable[..., dict[str, Any]], composition: str
) -> None:
    config = compose_config(composition, DOCUSEAL_HOST="sign.example.com")
    assert preflight.validate(config, preflight.ROOT) == [
        "DOCUSEAL_HOST is unused: the composition has no DocuSeal signing port, "
        "so leave it empty and the public gateway routes no signing paths"
    ]


@pytest.mark.usefixtures("signing_composition")
def test_signing_port_links_use_the_public_signing_host(
    compose_config: Callable[..., dict[str, Any]],
) -> None:
    config = compose_config("hive", DOCUSEAL_HOST="sign.example.com")
    assert preflight.validate(config, preflight.ROOT) == []
    assert preflight._port_signing_url("https://sign.example.com") == (
        "https://sign.example.com/s/preflight"
    )


@pytest.mark.usefixtures("signing_composition")
@pytest.mark.parametrize(
    ("host", "app_url", "problem"),
    [
        ("", None, NOT_PUBLIC),
        ("sign.localhost", None, NOT_PUBLIC),
        ("sign.example.invalid", None, NOT_PUBLIC),
        ("203.0.113.7", None, NOT_PUBLIC),
        ("sign.example.com", "http://127.0.0.1:3002", LINKS_OFF_HOST),
        ("sign.example.com", "", f"{EMPTY_APP_URL}|{LINKS_OFF_HOST}"),
        ("sign.example.com", "http://sign.example.com", LINKS_OFF_HOST),
        ("sign.example.com", "https://other.example.com", LINKS_OFF_HOST),
    ],
)
def test_signing_port_rejects_links_off_the_public_host(
    compose_config: Callable[..., dict[str, Any]],
    host: str,
    app_url: str | None,
    problem: str,
) -> None:
    config = compose_config("hive", DOCUSEAL_HOST=host)
    if app_url is not None:
        config["services"]["docuseal"]["environment"]["APP_URL"] = app_url
    assert preflight.validate(config, preflight.ROOT) == problem.split("|")


@pytest.mark.usefixtures("signing_composition")
def test_signing_host_must_differ_from_the_owner_host(
    compose_config: Callable[..., dict[str, Any]],
) -> None:
    config = compose_config(
        "hive",
        DOCUSEAL_HOST="biz.example.com",
        BIZ_RP_ID="biz.example.com",
        BIZ_ORIGIN="https://biz.example.com",
    )
    assert preflight.validate(config, preflight.ROOT) == [
        "DOCUSEAL_HOST must differ from the private owner hostname"
    ]


def test_preflight_warns_when_trusted_proxies_are_unset_or_too_broad(
    compose_config: Callable[..., dict[str, Any]],
) -> None:
    config = compose_config("demo")
    assert preflight.advise(config, {}) == [
        "BIZ_TRUSTED_PROXIES is empty, so the API trusts forwarded client IPs from every "
        "private range, including other Docker bridges. Set it to the gateway network "
        "172.30.250.0/24 (BIZ_APP_SUBNET)"
    ]
    config = compose_config("demo", BIZ_TRUSTED_PROXIES="172.30.250.0/24")
    assert preflight.validate(config, preflight.ROOT) == []
    assert preflight.advise(config, {}) == []
    config = compose_config("demo", BIZ_TRUSTED_PROXIES="10.0.0.0/8,not-a-network")
    assert preflight.advise(config, {}) == [
        "BIZ_TRUSTED_PROXIES has an entry that is not an IP network",
        "BIZ_TRUSTED_PROXIES does not cover the gateway network 172.30.250.0/24, so the API "
        "records the gateway's address for public requests",
        "BIZ_TRUSTED_PROXIES trusts addresses outside the gateway network 172.30.250.0/24",
    ]
    config = compose_config(
        "demo", BIZ_APP_SUBNET="10.42.0.0/24", BIZ_TRUSTED_PROXIES="10.42.0.0/24"
    )
    assert preflight.advise(config, {}) == []


def test_preflight_warns_about_legacy_stored_object_paths(
    compose_config: Callable[..., dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = compose_config("demo", BIZ_TRUSTED_PROXIES="172.30.250.0/24")
    assert preflight.advise(config, {"BIZ_STORAGE_ROOT": preflight.OBJECT_ROOT + "/"}) == []
    env_file = tmp_path / "legacy.env"
    env_file.write_text("# old layout\nexport BIZ_STORAGE_ROOT='/srv/biz/objects'\nJUNK\n")
    assert preflight.read_env_file(env_file) == {"BIZ_STORAGE_ROOT": "/srv/biz/objects"}

    monkeypatch.setattr(preflight, "resolve_compose", lambda *args: config)
    assert preflight.main(["--env-file", str(env_file)]) == 0
    result = capsys.readouterr()
    assert "Preflight passed" in result.out
    assert (
        "Preflight warning: BIZ_STORAGE_ROOT in the env file names a legacy storage path"
        in result.err
    )
    assert "Migrating stored object paths" in result.err
