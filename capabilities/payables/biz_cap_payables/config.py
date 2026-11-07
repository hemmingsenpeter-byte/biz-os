"""Composition-level config for payables (`payables:` in policy.yaml), bound at runtime."""

from __future__ import annotations

from typing import Any

_manifest: Any = None


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def setting(name: str, default: Any) -> Any:
    if _manifest is None:
        return default
    return ((_manifest.policy or {}).get("payables") or {}).get(name, default)
