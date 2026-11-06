"""Gate configuration: composition policy (`comms.*`), or set directly at register time."""

from __future__ import annotations

from typing import Any

from biz_cap_comms.gate import DEFAULT_MAX_LENGTH, GateConfig

_manifest: Any = None
_override: GateConfig | None = None


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def configure(cfg: GateConfig | None) -> None:
    global _override
    _override = cfg


def gate_config() -> GateConfig:
    if _override is not None:
        return _override
    own = ((getattr(_manifest, "policy", None) or {}).get("comms")) or {}
    return GateConfig(
        banned_phrases=tuple(own.get("banned_phrases", ())),
        link_domains=tuple(own.get("link_domains", ())),
        max_length={**DEFAULT_MAX_LENGTH, **(own.get("max_length") or {})},
    )


DEFAULT_REPLY_TOKEN_DAYS = 14


def reply_token_days() -> float:
    """How long a reply token stays valid (`comms.reply_token_days` policy, default 14)."""
    own = ((getattr(_manifest, "policy", None) or {}).get("comms")) or {}
    try:
        days = float(own.get("reply_token_days", DEFAULT_REPLY_TOKEN_DAYS))
    except (TypeError, ValueError):
        return float(DEFAULT_REPLY_TOKEN_DAYS)
    return days if days > 0 else float(DEFAULT_REPLY_TOKEN_DAYS)
