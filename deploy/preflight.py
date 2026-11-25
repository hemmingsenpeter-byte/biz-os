"""Check a BIZ OS Compose installation without starting services or contacting providers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import time
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
import yaml
from biz_kernel.cli.main import doctor_cmd
from biz_kernel.loader import LoadError, load_composition
from biz_kernel.tools.shadow import mode_problems
from biz_port_sign_docuseal import DocuSealSign
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ROOT = Path(__file__).resolve().parents[1]
OBJECT_ROOT = "/var/lib/biz-os/objects"
# Compose gives the public gateway this reserved name when DOCUSEAL_HOST is empty, so the
# signing routes in PublicCaddyfile cannot match a real request.
SIGNING_DISABLED_HOST = "signing-disabled.invalid"
# Without a signing host, Compose points DocuSeal's APP_URL at its loopback admin port.
LOOPBACK_DOCUSEAL_URL = "http://127.0.0.1:3002"
SIGNING_GUARD = "not vars_regexp {http.request.orig_uri} "
# The exact RE2 guards the public signing routes must carry. They scan the whole raw
# path (Go keeps a raw # in it) and refuse escapes and dot-segments; /disk/ alone
# allows %2F, which its standard-Base64 keys need, away from slashes and dot-segments.
PATH_GUARD_REGEX = (
    r"(?i)^[^?]*(%(2e|2f|5c|25|23)|#|\\|//|/\.\.?(/|\?|$))|[?&](_|%5f)method([=&#]|$)"
)
DISK_GUARD_REGEX = (
    r"(?i)^[^?]*(%(2e|5c|25|23)|#|\\|(/|%2f)(/|%2f)|(/|%2f)\.\.?(/|%2f|\?|$))"
    r"|[?&](_|%5f)method([=&#]|$)"
)
PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".invalid", ".test", ".ts.net")
REQUIRED_PORT_ENV = {
    "accounting_xero": ("BIZ_XERO_CLIENT_ID", "BIZ_XERO_CLIENT_SECRET"),
    "mail_m365": (
        "BIZ_M365_TENANT",
        "BIZ_M365_CLIENT_ID",
        "BIZ_M365_SECRET",
        "BIZ_M365_MAILBOX",
        "BIZ_M365_TZ",
    ),
    "model_claude": ("ANTHROPIC_API_KEY",),
    "model_ollama": ("BIZ_OLLAMA_URL", "BIZ_OLLAMA_MODEL", "BIZ_OLLAMA_EMBED_MODEL"),
    "sign_docuseal": (
        "BIZ_DOCUSEAL_URL",
        "BIZ_DOCUSEAL_API_KEY",
        "BIZ_DOCUSEAL_WEBHOOK_SECRET",
        "BIZ_STORAGE_ROOT",
    ),
    "storage_local": ("BIZ_STORAGE_ROOT",),
    "stt_whisper": ("BIZ_STORAGE_ROOT",),
    "telephony_twilio": (
        "BIZ_TWILIO_SID",
        "BIZ_TWILIO_TOKEN",
        "BIZ_TWILIO_FROM",
        "BIZ_TWILIO_WEBHOOK_URL",
    ),
}
OPTIONAL_PORT_ENV = {
    "accounting_xero": (
        "BIZ_XERO_TENANT_ID",
        "BIZ_XERO_SALES_ACCOUNT",
        "BIZ_XERO_PURCHASES_ACCOUNT",
    ),
    "mail_m365": (
        "BIZ_M365_WORK_START",
        "BIZ_M365_WORK_END",
        "BIZ_M365_AUTHSERV_ID",
    ),
    "model_claude": ("BIZ_CLAUDE_MODEL",),
    "sign_docuseal": (
        "BIZ_DOCUSEAL_TEMPLATE_ID",
        "BIZ_DOCUSEAL_ALLOW_TEMPLATE_MODE",
        "BIZ_DOCUSEAL_WEBHOOK_HEADER",
    ),
    "stt_whisper": ("BIZ_WHISPER_MODEL", "BIZ_WHISPER_DEVICE"),
}
CORE_ENV = ("BIZ_APP_DIR", "BIZ_DATABASE_URL", "BIZ_ORIGIN", "BIZ_RP_ID", "BIZ_SECRET_KEY")


def resolve_compose(compose_file: Path, env_file: Path) -> dict[str, Any]:
    # Compose resolves YAML anchors, interpolation and mounts with actual CLI semantics.
    # Keep online build, pull and remote file resolution outside this check.
    source = yaml.safe_load(compose_file.read_text())
    if (
        not isinstance(source, dict)
        or "include" in source
        or any("extends" in service for service in source.get("services", {}).values())
    ):
        raise ValueError("preflight requires a self-contained local Compose file")
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--env-file",
            str(env_file),
            "--profile",
            "*",
            "config",
            "--no-path-resolution",
            "--format",
            "json",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    config = json.loads(completed.stdout)
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        raise ValueError("invalid Compose configuration")
    return config


def _configured(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.strip().lower() not in {"", "change-me", "changeme", "replace-me"}
        and not (value.startswith("<") and value.endswith(">"))
    )


def _http_url(value: str) -> SplitResult | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
        or any(character.isspace() for character in value)
    ):
        return None
    return parsed


def _validate_app_urls(environment: dict[str, str], *, demo: bool, problems: list[str]) -> None:
    origin = _http_url(environment.get("BIZ_ORIGIN", ""))
    rp_id = environment.get("BIZ_RP_ID", "").lower()
    if (
        not origin
        or origin.path
        or (origin.scheme == "http" and (not demo or origin.hostname != "localhost"))
    ):
        problems.append("BIZ_ORIGIN must be an HTTPS browser origin or http://localhost for demo")
    elif origin.hostname != rp_id:
        problems.append("BIZ_RP_ID must match the BIZ_ORIGIN host")


def _validate_database(
    url: str, postgres: dict[str, str], database: str, variable: str, problems: list[str]
) -> None:
    password = postgres.get("POSTGRES_PASSWORD", "")
    try:
        parsed = make_url(url)
        valid = (
            parsed.get_backend_name() == "postgresql"
            and parsed.username == postgres.get("POSTGRES_USER") == "biz"
            and parsed.password == password
            and parsed.host == "postgres"
            and parsed.port in {None, 5432}
            and parsed.database == database
        )
    except (ArgumentError, ValueError):
        valid = False
    if not _configured(password):
        problems.append("POSTGRES_PASSWORD must be set")
    elif not re.fullmatch(r"[a-zA-Z0-9._~-]+", password):
        problems.append("POSTGRES_PASSWORD must use URL-safe characters")
    elif not valid:
        problems.append(f"{variable} must match the Compose postgres credentials and database")


def _validate_port_values(
    ports: set[str], environment: dict[str, str], problems: list[str]
) -> None:
    if "mail_m365" in ports:
        try:
            ZoneInfo(environment.get("BIZ_M365_TZ") or "UTC")
        except (ValueError, ZoneInfoNotFoundError):
            problems.append("BIZ_M365_TZ must be a valid IANA time zone")
        start = environment.get("BIZ_M365_WORK_START", "09:00")
        end = environment.get("BIZ_M365_WORK_END", "17:00")
        try:
            valid_hours = (
                re.fullmatch(r"\d\d:\d\d", start)
                and re.fullmatch(r"\d\d:\d\d", end)
                and time.fromisoformat(start) < time.fromisoformat(end)
            )
        except ValueError:
            valid_hours = False
        if not valid_hours:
            problems.append(
                "BIZ_M365_WORK_START and BIZ_M365_WORK_END must be ordered HH:MM values"
            )

    if "telephony_twilio" in ports:
        url = _http_url(environment.get("BIZ_TWILIO_WEBHOOK_URL", ""))
        if not url or url.scheme != "https" or url.path != "/webhooks/telephony":
            problems.append("BIZ_TWILIO_WEBHOOK_URL must be an HTTPS /webhooks/telephony URL")

    if "model_ollama" in ports:
        url = _http_url(environment.get("BIZ_OLLAMA_URL", ""))
        if not url or url.hostname in {"localhost", "127.0.0.1", "::1"}:
            problems.append("BIZ_OLLAMA_URL must address Ollama from inside the containers")

    if "sign_docuseal" in ports:
        if not _http_url(environment.get("BIZ_DOCUSEAL_URL", "")):
            problems.append("BIZ_DOCUSEAL_URL must be an HTTP or HTTPS URL")
        template = environment.get("BIZ_DOCUSEAL_TEMPLATE_ID", "")
        if template and not re.fullmatch(r"[1-9]\d*", template):
            problems.append("BIZ_DOCUSEAL_TEMPLATE_ID must be a positive integer when set")
        if environment.get("BIZ_DOCUSEAL_ALLOW_TEMPLATE_MODE", "0") not in {"0", "1"}:
            problems.append("BIZ_DOCUSEAL_ALLOW_TEMPLATE_MODE must be 0 or 1")


def _public_hostname(host: str) -> bool:
    labels = host.split(".")
    if len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        return False
    return not host.endswith(PRIVATE_SUFFIXES) and not labels[-1].isdigit()


def _port_signing_url(app_url: str) -> str | None:
    """The link the sign_docuseal port returns for a pinned DocuSeal create response.

    DocuSeal builds each submitter's embed_src from the scheme, host and port of its
    APP_URL; the port passes that link through, or falls back to a relative /s/<slug>.
    """
    parsed = _http_url(app_url)
    if parsed is None:
        return None
    created = [
        {
            "submission_id": 1,
            "slug": "preflight",
            "embed_src": f"{parsed.scheme}://{parsed.netloc}/s/preflight",
        }
    ]
    return DocuSealSign._parse_created(created)[1]


def _validate_signing(
    services: dict[str, Any], ports: set[str], api: dict[str, str], problems: list[str]
) -> None:
    gateway = services.get("tunnel-ingress", {}).get("environment") or {}
    host = str(gateway.get("DOCUSEAL_HOST", "")).strip().lower()
    app_url = str((services["docuseal"].get("environment") or {}).get("APP_URL") or "")
    enabled = host not in {"", SIGNING_DISABLED_HOST}
    if not _http_url(app_url):
        # DocuSeal reads an empty APP_URL as its default origin and builds hostless links.
        problems.append("DocuSeal APP_URL must be a non-empty HTTP or HTTPS origin")
    if "sign_docuseal" not in ports:
        if enabled or app_url != LOOPBACK_DOCUSEAL_URL:
            problems.append(
                "DOCUSEAL_HOST is unused: the composition has no DocuSeal signing port, "
                "so leave it empty and the public gateway routes no signing paths"
            )
        return
    if not enabled or not _public_hostname(host):
        problems.append("DOCUSEAL_HOST must be the public signing hostname for DocuSeal signing")
        return
    if host == api.get("BIZ_RP_ID", "").lower():
        problems.append("DOCUSEAL_HOST must differ from the private owner hostname")
    signing = _http_url(_port_signing_url(app_url) or "")
    if (
        not signing
        or signing.scheme != "https"
        or signing.hostname != host
        or signing.port not in {None, 443}
    ):
        problems.append(
            "DocuSeal signing links must use https://DOCUSEAL_HOST (set docuseal APP_URL from it)"
        )


def _validate_storage(
    services: dict[str, Any], compose_dir: Path, *, require_existing: bool, problems: list[str]
) -> None:
    storage_sources = []
    for name in ("biz-api", "biz-worker"):
        service = services[name]
        mounts = [mount for mount in service.get("volumes", []) if mount["target"] == OBJECT_ROOT]
        if (
            service.get("environment", {}).get("BIZ_STORAGE_ROOT") != OBJECT_ROOT
            or len(mounts) != 1
            or mounts[0]["type"] != "bind"
            or mounts[0].get("read_only")
        ):
            problems.append(f"{name} needs a writable bind mount at BIZ_STORAGE_ROOT")
            continue
        storage_sources.append(Path(mounts[0]["source"]))
    if len(storage_sources) != 2:
        return
    first, second = storage_sources
    canonical = (compose_dir / first).resolve()
    if first != second or (require_existing and not first.is_absolute()) or canonical == Path("/"):
        problems.append("BIZ_STORAGE_HOST_ROOT must be one shared, absolute host directory")
        return
    try:
        mode = canonical.stat().st_mode
        if not stat.S_ISDIR(mode) or require_existing and stat.S_IMODE(mode) != 0o700:
            problems.append("BIZ_STORAGE_HOST_ROOT must be a directory with mode 0700")
    except FileNotFoundError:
        if require_existing:
            problems.append("BIZ_STORAGE_HOST_ROOT must already exist")
    for name, service in services.items():
        if name in {"biz-api", "biz-worker"}:
            continue
        for mount in service.get("volumes", []):
            if mount.get("type") == "bind":
                source = (compose_dir / mount["source"]).resolve()
                if canonical.is_relative_to(source) or source.is_relative_to(canonical):
                    problems.append(
                        "Only biz-api and biz-worker may mount the object storage directory"
                    )
                    return


def _loopback(address: str) -> bool:
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


def _validate_routes(
    services: dict[str, Any],
    networks: dict[str, Any],
    root: Path,
    compose_dir: Path,
    problems: list[str],
) -> None:
    for name, service in services.items():
        if service.get("network_mode") == "host" or any(
            not _loopback(port.get("host_ip", "")) for port in service.get("ports", [])
        ):
            problems.append(f"{name} may publish host ports only on loopback")
        # Joining another namespace inherits its networks and bypasses the checks below;
        # container names cannot be mapped back to services, so reject both forms.
        mode = str(service.get("network_mode", ""))
        if mode.startswith(("service:", "container:")):
            problems.append(f"{name} must not join another network namespace ({mode})")
        if service.get("image") and not re.fullmatch(r".+@sha256:[0-9a-f]{64}", service["image"]):
            problems.append(f"{name} image must be pinned by digest")

    expected = {"cloudflared": {"tunnel"}, "tunnel-ingress": {"default", "tunnel"}}
    for name, private_networks in expected.items():
        service = services.get(name, {})
        if (
            set(service.get("networks", {})) != private_networks
            or service.get("network_mode")
            or service.get("ports")
            or service.get("profiles") != ["public"]
        ):
            problems.append(f"{name} must use its isolated network and opt-in public profile")
    if (
        any(
            "tunnel" in service.get("networks", {})
            for name, service in services.items()
            if name not in expected
        )
        or (tunnel := networks.get("tunnel", {})).get("external")
        or tunnel.get("driver", "bridge") != "bridge"
    ):
        problems.append("The tunnel bridge must contain only cloudflared and tunnel-ingress")

    public_mounts = services.get("tunnel-ingress", {}).get("volumes", [])
    if not any(
        mount.get("target") == "/etc/caddy/Caddyfile"
        and mount.get("type") == "bind"
        and mount.get("read_only")
        and (compose_dir / mount["source"]).resolve()
        == (root / "deploy" / "PublicCaddyfile").resolve()
        for mount in public_mounts
    ):
        problems.append("tunnel-ingress must use the dedicated read-only PublicCaddyfile")
    problems.extend(public_caddyfile_problems((root / "deploy" / "PublicCaddyfile").read_text()))


def public_caddyfile_problems(caddyfile: str) -> list[str]:
    """Keep the raw-URI guards on every public route that reaches DocuSeal.

    Caddy matches the decoded, cleaned path but forwards the raw one, so a signing
    route without these guards lets escaped dot-segments reach other DocuSeal routes.
    """
    matchers = dict(re.findall(r"\n\t@([\w-]+) \{\n(.*?)\n\t\}", caddyfile, re.S))
    handlers = re.findall(r"\n\thandle(?: @([\w-]+))? \{\n(.*?)\n\t\}", caddyfile, re.S)
    problems = []
    # Fail closed: every upstream mention must sit inside a handle block checked below,
    # so a route written as handle_path, route, an inline matcher or a bare
    # reverse_proxy cannot reach DocuSeal unguarded.
    code = "\n".join(line.split("#", 1)[0] for line in caddyfile.splitlines())
    mentions = len(re.findall(r"\bdocuseal\b", code, re.I))
    checked = sum(
        len(
            re.findall(
                r"\bdocuseal\b", "\n".join(ln.split("#", 1)[0] for ln in body.splitlines()), re.I
            )
        )
        for _, body in handlers
    )
    if mentions != checked:
        problems.append(
            "PublicCaddyfile may reach DocuSeal only from guarded `handle @name` blocks"
        )
    for name, body in handlers:
        if "docuseal:" not in body:
            continue
        matcher = matchers.get(name, "")
        expected = DISK_GUARD_REGEX if name == "signing_disk" else PATH_GUARD_REGEX
        guards = re.findall(re.escape(SIGNING_GUARD) + r"`([^`\n]*)`", matcher)
        guarded = (
            "host {$DOCUSEAL_HOST:" in matcher
            and guards == [expected]
            and "not header X-HTTP-Method-Override *" in matcher
            and "header_up -X-HTTP-Method-Override" in body
            and "uri query -_method" in body
        )
        if not guarded:
            problems.append(
                f"PublicCaddyfile route {name or '(default)'} must keep the signing host, "
                "raw-URI and method-override guards"
            )
    return problems


@contextlib.contextmanager
def _without_host_settings() -> Any:
    """Doctor judges the composition alone; resolved Compose settings are checked separately."""
    keys = ("BIZ_EXECUTION_MODE", "BIZ_OLLAMA_URL", "BIZ_OLLAMA_MODEL", "BIZ_OLLAMA_EMBED_MODEL")
    saved = {key: os.environ.pop(key) for key in keys if key in os.environ}
    try:
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(saved)


def validate(config: dict[str, Any], root: Path, *, compose_dir: Path | None = None) -> list[str]:
    compose_dir = compose_dir or root / "deploy"
    services = config["services"]
    expected = {"biz-api", "biz-worker", "biz-web", "postgres", "docuseal"}
    if expected - services.keys():
        return ["Compose must define biz-api, biz-worker, biz-web, postgres and docuseal"]

    api = services["biz-api"].get("environment", {})
    worker = services["biz-worker"].get("environment", {})
    postgres = services["postgres"].get("environment", {})
    composition = re.fullmatch(r"/app/apps/([a-zA-Z0-9_-]+)", api.get("BIZ_APP_DIR", ""))
    if not composition:
        return ["BIZ_APP_DIR must select a composition under /app/apps/"]
    app_dir = root / "apps" / composition[1]
    try:
        manifest = load_composition(app_dir, root)
        _, composition_mode = mode_problems(
            manifest.execution_mode,
            manifest.supervised_authorization,
            override=None,
            ports=manifest.ports,
            policy=manifest.policy,
            environ={},
        )
        if composition_mode:  # named precisely: these are owner decisions, not typos
            return composition_mode
        # The existing doctor checks port and capability imports, budgets, schedules and policy.
        with (
            _without_host_settings(),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            doctor_cmd(app_dir, root)
    except (LoadError, OSError, typer.Exit, ValueError, TypeError, yaml.YAMLError):
        return ["The selected composition or policy failed validation. Run biz doctor locally."]

    ports = set(manifest.ports.values())
    demo = all(port.startswith("fake_") for port in ports)
    problems: list[str] = []
    required = set(CORE_ENV)
    optional = {"BIZ_EXECUTION_MODE"}
    for port in sorted(ports):
        if port.startswith("fake_"):
            continue
        if port not in REQUIRED_PORT_ENV:
            problems.append("The selected composition uses a port with no deployment env checks")
            continue
        required.update(REQUIRED_PORT_ENV[port])
        optional.update(OPTIONAL_PORT_ENV.get(port, ()))
    for key in sorted(required | optional):
        if key not in api or key not in worker:
            problems.append(f"{key} must be mapped for both biz-api and biz-worker")
        elif api[key] != worker[key]:
            problems.append(f"{key} must match between biz-api and biz-worker")
        elif key in required and not _configured(api[key]):
            problems.append(f"{key} must be set for the selected composition")

    if api.get("BIZ_ORIGIN") != services["biz-web"].get("environment", {}).get("BIZ_ORIGIN"):
        problems.append("BIZ_ORIGIN must also match the owner console")
    _, mode_issues = mode_problems(
        manifest.execution_mode,
        manifest.supervised_authorization,
        override=api.get("BIZ_EXECUTION_MODE") or None,
        ports=manifest.ports,
        policy=manifest.policy,
        environ=api,
        compose=True,
    )
    problems.extend(mode_issues)
    if api.get("BIZ_DBOS_SYSTEM_URL") or worker.get("BIZ_DBOS_SYSTEM_URL"):
        problems.append(
            "BIZ_DBOS_SYSTEM_URL must be empty so backups include the workflow database"
        )
    _validate_app_urls(api, demo=demo, problems=problems)
    _validate_database(
        api.get("BIZ_DATABASE_URL", ""), postgres, "biz", "BIZ_DATABASE_URL", problems
    )
    if "sign_docuseal" in ports:
        docuseal = services["docuseal"].get("environment", {})
        if not _configured(docuseal.get("SECRET_KEY_BASE", "")):
            problems.append("DOCUSEAL_SECRET_KEY_BASE must be set when using DocuSeal")
        _validate_database(
            docuseal.get("DATABASE_URL", ""),
            postgres,
            "docuseal",
            "DocuSeal DATABASE_URL",
            problems,
        )
    _validate_port_values(ports, api, problems)
    _validate_signing(services, ports, api, problems)
    _validate_storage(services, compose_dir, require_existing=not demo, problems=problems)
    _validate_routes(services, config.get("networks", {}), root, compose_dir, problems)
    return problems


def _gateway_subnet(config: dict[str, Any]) -> IPv4Network | IPv6Network | None:
    default = (config.get("networks") or {}).get("default") or {}
    pools = (default.get("ipam") or {}).get("config") or []
    subnets = [pool.get("subnet") for pool in pools if isinstance(pool, dict)]
    if len(subnets) != 1 or not isinstance(subnets[0], str):
        return None
    try:
        return ip_network(subnets[0], strict=False)
    except ValueError:
        return None


def _covers(outer: IPv4Network | IPv6Network, inner: IPv4Network | IPv6Network) -> bool:
    if isinstance(outer, IPv4Network) and isinstance(inner, IPv4Network):
        return inner.subnet_of(outer)
    if isinstance(outer, IPv6Network) and isinstance(inner, IPv6Network):
        return inner.subnet_of(outer)
    return False


def _advise_trusted_proxies(config: dict[str, Any], warnings: list[str]) -> None:
    api = config["services"].get("biz-api", {}).get("environment") or {}
    raw = str(api.get("BIZ_TRUSTED_PROXIES") or "").strip()
    gateway = _gateway_subnet(config)
    named = f"{gateway} (BIZ_APP_SUBNET)" if gateway else "shared by caddy and tunnel-ingress"
    if not raw:
        warnings.append(
            "BIZ_TRUSTED_PROXIES is empty, so the API trusts forwarded client IPs from every "
            f"private range, including other Docker bridges. Set it to the gateway network {named}"
        )
        return
    networks: list[IPv4Network | IPv6Network] = []
    for item in raw.split(","):
        try:
            networks.append(ip_network(item.strip(), strict=False))
        except ValueError:
            warnings.append("BIZ_TRUSTED_PROXIES has an entry that is not an IP network")
    if gateway is None:
        return
    if not any(_covers(network, gateway) for network in networks):
        warnings.append(
            f"BIZ_TRUSTED_PROXIES does not cover the gateway network {gateway}, so the API "
            "records the gateway's address for public requests"
        )
    if any(not _covers(gateway, network) for network in networks):
        warnings.append(
            f"BIZ_TRUSTED_PROXIES trusts addresses outside the gateway network {gateway}"
        )


def advise(config: dict[str, Any], env_values: Mapping[str, str]) -> list[str]:
    """Settings that work but deserve the operator's attention. These never fail preflight."""
    warnings: list[str] = []
    _advise_trusted_proxies(config, warnings)
    legacy = env_values.get("BIZ_STORAGE_ROOT", "").strip()
    if legacy and legacy.rstrip("/") != OBJECT_ROOT:
        warnings.append(
            "BIZ_STORAGE_ROOT in the env file names a legacy storage path. Stored file:// "
            f"URIs under it will not resolve at {OBJECT_ROOT}; follow 'Migrating stored "
            "object paths' in deploy/README.md before starting the API and worker"
        )
    return warnings


def read_env_file(env_file: Path) -> dict[str, str]:
    """KEY=VALUE lines of a Compose env file; comments and malformed lines are skipped."""
    values: dict[str, str] = {}
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / "deploy" / ".env")
    parser.add_argument("--compose-file", type=Path, default=ROOT / "deploy" / "docker-compose.yml")
    args = parser.parse_args(argv)
    warnings: list[str] = []
    try:
        config = resolve_compose(args.compose_file, args.env_file)
        problems = validate(config, ROOT, compose_dir=args.compose_file.resolve().parent)
        warnings = advise(config, read_env_file(args.env_file))
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
    ):
        # Compose and schema errors may contain secret values. Never print raw exceptions.
        problems = ["Could not read and validate the local Compose configuration"]
    for warning in warnings:
        print(f"Preflight warning: {warning}", file=sys.stderr)
    if problems:
        for problem in problems:
            print(f"Preflight failed: {problem}", file=sys.stderr)
        return 1
    print("Preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
