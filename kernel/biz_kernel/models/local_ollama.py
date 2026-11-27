"""Verify a pinned installed model on a literal loopback Ollama, without proxy routing."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

BACKEND = "biz_port_model_ollama.OllamaModel"
_DISTRIBUTION = "biz-port-model-ollama"
_DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")


class LocalModelError(ValueError):
    """The requested backend or installed model could not be verified safely."""


def literal_loopback_url(value: str) -> str:
    """Accept only a literal loopback address; no gateway, DNS, credentials or redirects."""
    message = "local comparison needs a literal loopback Ollama URL without credentials or paths"
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or any(c.isspace() for c in value)
            or parsed.scheme not in {"http", "https"}
            or not ip_address(parsed.hostname or "").is_loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port == 0
        ):
            raise LocalModelError(message)
    except (ValueError, TypeError, AttributeError) as exc:
        raise LocalModelError(message) from exc
    return value.rstrip("/")


def adapter_version() -> str:
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise LocalModelError("the installed local model adapter version is unavailable") from exc


def verify_local_ollama(
    *,
    base_url: str,
    model: str,
    digest: str,
    ollama_version: str | None = None,
    backend_version: str | None = None,
) -> dict[str, Any]:
    """Verify metadata as the scenario probe does, before any private inference payload.

    Re-check this at inference boundaries; a configured model tag is not an immutable
    revision. The metadata request itself always uses direct loopback and rejects redirects.
    """
    endpoint = literal_loopback_url(base_url)
    if not isinstance(model, str) or not model.strip() or "cloud" in model.lower():
        raise LocalModelError("remote model routes are not supported")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise LocalModelError("an exact installed model SHA-256 digest is required")
    installed_backend = adapter_version()
    if backend_version is not None and backend_version != installed_backend:
        raise LocalModelError("the installed local model adapter version changed")
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            tags = client.get(endpoint + "/api/tags")
            tags.raise_for_status()
            entries = [row for row in tags.json()["models"] if row.get("name") == model]
            if len(entries) != 1 or entries[0].get("digest") != digest:
                raise LocalModelError("model digest does not match the local installation")
            entry = entries[0]
            if entry.get("remote_host") or entry.get("remote_model"):
                raise LocalModelError("remote model routes are not supported")
            response = client.get(endpoint + "/api/version")
            response.raise_for_status()
            installed_ollama = response.json()["version"]
            if not isinstance(installed_ollama, str) or not installed_ollama.strip():
                raise LocalModelError("the installed Ollama version is unavailable")
            if ollama_version is not None and installed_ollama != ollama_version:
                raise LocalModelError("the installed Ollama version changed")
    except (httpx.HTTPError, KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, LocalModelError):
            raise
        raise LocalModelError("local Ollama metadata could not be verified") from exc
    return {
        "backend": BACKEND,
        "backend_version": installed_backend,
        "base_url": endpoint,
        "name": model,
        "digest": digest,
        "ollama_version": installed_ollama,
    }
