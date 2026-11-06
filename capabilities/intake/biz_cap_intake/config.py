"""Composition-level config for intake, bound from the runtime's manifest."""

from __future__ import annotations

from typing import Any

SLOT = "industry.text_back_template"
_manifest: Any = None


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def text_back_template() -> str:
    """Composition policy (`intake.text_back_template`) wins, else the industry slot fill."""
    if _manifest is None:
        raise RuntimeError("intake is not bound to a composition")
    own = ((_manifest.policy or {}).get("intake") or {}).get("text_back_template")
    if own:
        return str(own).strip()
    fill = _manifest.slot_fills.get(SLOT)
    if fill is None:
        raise RuntimeError(
            f"no text-back template: composition has no intake policy and {SLOT} is unfilled"
        )
    return fill[1].read_text().strip()


def setting(name: str, default: Any) -> Any:
    if _manifest is None:
        return default
    return ((_manifest.policy or {}).get("intake") or {}).get(name, default)
