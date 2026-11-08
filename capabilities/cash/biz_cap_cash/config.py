"""Composition-level config for cash (`cash:` in policy.yaml), bound at runtime."""

from __future__ import annotations

from typing import Any

_manifest: Any = None


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def setting(name: str, default: Any) -> Any:
    if _manifest is None:
        return default
    return ((_manifest.policy or {}).get("cash") or {}).get(name, default)


def day_timezone() -> str:
    """The business's local day, shared with the money limits (`limits.day_timezone`)."""
    if _manifest is None:
        return "UTC"
    return str(((_manifest.policy or {}).get("limits") or {}).get("day_timezone", "UTC"))
