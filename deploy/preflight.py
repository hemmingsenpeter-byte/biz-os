"""Check a BIZ OS Compose installation without starting services or contacting providers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import stat
import subprocess
import sys
from datetime import time
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
import yaml
from biz_kernel.cli.main import doctor_cmd
from biz_kernel.loader import LoadError, load_composition
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ROOT = Path(__file__).resolve().parents[1]
OBJECT_ROOT = "/var/lib/biz-os/objects"
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
        # The existing doctor checks port and capability imports, budgets, schedules and policy.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
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
    mode = api.get("BIZ_EXECUTION_MODE") or manifest.execution_mode
    if mode not in {"shadow", "supervised"}:
        problems.append("BIZ_EXECUTION_MODE must be shadow or supervised")
    if mode == "shadow" and "model_ollama" in ports:
        ollama = _http_url(api.get("BIZ_OLLAMA_URL", ""))
        if not ollama or ollama.hostname != "host.docker.internal":
            problems.append("Shadow Ollama must use the appliance host.docker.internal endpoint")
        for key in ("BIZ_OLLAMA_MODEL", "BIZ_OLLAMA_EMBED_MODEL"):
            if re.search(r"(?:^|[/:_-])cloud(?:$|[/:])", api.get(key, ""), re.I):
                problems.append(f"{key} cannot use a cloud model in shadow mode")
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
    _validate_storage(services, compose_dir, require_existing=not demo, problems=problems)
    _validate_routes(services, config.get("networks", {}), root, compose_dir, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / "deploy" / ".env")
    parser.add_argument("--compose-file", type=Path, default=ROOT / "deploy" / "docker-compose.yml")
    args = parser.parse_args(argv)
    try:
        config = resolve_compose(args.compose_file, args.env_file)
        problems = validate(config, ROOT, compose_dir=args.compose_file.resolve().parent)
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
    if problems:
        for problem in problems:
            print(f"Preflight failed: {problem}", file=sys.stderr)
        return 1
    print("Preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
