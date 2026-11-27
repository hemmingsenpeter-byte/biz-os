"""Resolved Ollama settings and offline appliance-locality checks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
_CLOUD = re.compile(r"(?:^|[^a-z0-9])cloud(?:$|[^a-z0-9])", re.IGNORECASE)


def appliance_endpoint(url: Any, *, compose: bool = False) -> bool:
    """Only literal loopback or Docker's appliance host, with no URL credentials."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            return False
        if compose:
            return host == "host.docker.internal"
        if host in {"localhost", "host.docker.internal"}:
            return True
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OllamaSettings:
        env = os.environ if environ is None else environ
        return cls(
            env.get("BIZ_OLLAMA_URL", DEFAULT_URL),
            env.get("BIZ_OLLAMA_MODEL", DEFAULT_MODEL),
            env.get("BIZ_OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        )

    def locality_problems(self, *, compose: bool = False) -> list[str]:
        problems = []
        if not appliance_endpoint(self.base_url, compose=compose):
            problems.append(
                "Shadow Ollama must use the appliance host.docker.internal endpoint"
                if compose
                else "Shadow Ollama must use a loopback or appliance host.docker.internal endpoint"
            )
        for key, model in (
            ("BIZ_OLLAMA_MODEL", self.model),
            ("BIZ_OLLAMA_EMBED_MODEL", self.embed_model),
        ):
            if not isinstance(model, str) or not model.strip():
                problems.append(f"{key} must name an installed local model in shadow mode")
            elif _CLOUD.search(model):
                problems.append(f"{key} cannot use a cloud model in shadow mode")
        return problems
