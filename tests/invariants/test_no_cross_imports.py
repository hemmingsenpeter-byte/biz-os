"""A capability never imports another capability's modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _capability_of(module: str) -> str | None:
    """Capability a dotted module path belongs to, if any."""
    parts = module.split(".")
    if parts[0].startswith("biz_cap_"):
        return parts[0][len("biz_cap_") :]
    if parts[0] == "capabilities" and len(parts) > 1:
        return parts[1]
    return None


def _violations(cap_dir: Path, others: set[str]) -> list[str]:
    bad = []
    for py in cap_dir.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                if _capability_of(m) in others:
                    bad.append(f"{py.relative_to(ROOT)}: imports {m}")
    return sorted(set(bad))


def test_no_cross_imports() -> None:
    caps = [
        d for d in (ROOT / "capabilities").iterdir() if d.is_dir() and not d.name.startswith("_")
    ]
    names = {d.name for d in caps}
    bad = [v for d in caps for v in _violations(d, names - {d.name})]
    assert not bad, "\n".join(bad)


def test_detector_catches_cross_import(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "m.py").write_text("from biz_cap_b.tools import x\n")
    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(__import__(__name__), "ROOT", tmp_path, raising=False)
        assert _violations(tmp_path / "a", {"b"})
