"""Kernel, capabilities, ports and roles contain no company, industry or price words."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ["kernel", "capabilities", "ports", "roles"]
SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sql", ".sh"}
SKIP_PARTS = {"__pycache__", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}


def _patterns() -> list[re.Pattern[str]]:
    out = []
    for line in (ROOT / "tests" / "banned_words.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        body = line[3:] if line.startswith("re:") else rf"\b{re.escape(line)}\b"
        out.append(re.compile(body, re.IGNORECASE))
    return out


def _files() -> list[Path]:
    files = []
    for top in SCANNED:
        for p in (ROOT / top).rglob("*"):
            rel = p.relative_to(ROOT)
            if not p.is_file() or p.suffix not in SUFFIXES or SKIP_PARTS & set(rel.parts):
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            # test code may mention words; role golden tests are data and stay scanned
            if "tests" in rel.parts and rel.parts[0] != "roles":
                continue
            files.append(p)
    return files


def test_banned_word_list_is_not_empty() -> None:
    assert len(_patterns()) >= 5


def test_no_business_words() -> None:
    pats = _patterns()
    hits = []
    for p in _files():
        for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            for pat in pats:
                if pat.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {pat.pattern}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)
