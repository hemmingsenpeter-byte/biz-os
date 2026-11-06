"""Where templates may come from: slot fills of the bound composition, plus extra roots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_manifest: Any = None
_roots: list[Path] = []


def bind(manifest: Any) -> None:
    global _manifest
    _manifest = manifest


def add_root(path: str | Path) -> None:
    _roots.append(Path(path).resolve())


def reset() -> None:
    global _manifest
    _manifest = None
    _roots.clear()


def _fills() -> dict[str, Path]:
    if _manifest is None:
        return {}
    return {slot: Path(fill[1]).resolve() for slot, fill in _manifest.slot_fills.items()}


def resolve_template(ref: str) -> Path:
    """A slot name (industry.scope_template) or a path inside a template root.

    Industry templates are data, but a model-chosen path must never read arbitrary files:
    only the directories of slot fills and explicitly added roots are readable.
    """
    fills = _fills()
    if ref in fills:
        return fills[ref]
    roots = [*{p.parent for p in fills.values()}, *_roots]
    candidates = [Path(ref)] if Path(ref).is_absolute() else [r / ref for r in roots]
    for cand in candidates:
        path = cand.resolve()
        if path.is_file() and any(path.is_relative_to(r) for r in roots):
            return path
    raise LookupError(f"template {ref!r} is not a slot fill or a file in a template root")
