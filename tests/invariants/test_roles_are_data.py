"""Role packs hold YAML and Markdown only: no Python. Code never names a role."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = {".py", ".pyi", ".js", ".ts", ".sh", ".so"}
# Shared layers must work for any role pack, so their code reads roles as data.
SHARED = ["kernel", "capabilities", "ports"]
SKIP_PARTS = {"__pycache__", ".venv", "tests"}


def test_roles_are_data() -> None:
    code = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "roles").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and (p.suffix in CODE or p.suffix == ".pyc")
    ]
    assert not code, code


def test_shared_code_names_no_role() -> None:
    roles = sorted(p.name for p in (ROOT / "roles").iterdir() if p.is_dir())
    assert roles
    pattern = re.compile(rf"\b(?:{'|'.join(map(re.escape, roles))})\b", re.IGNORECASE)
    hits = [
        f"{p.relative_to(ROOT)}:{n}: {line.strip()[:80]}"
        for top in SHARED
        for p in (ROOT / top).rglob("*.py")
        if not SKIP_PARTS & set(p.relative_to(ROOT).parts)
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, "\n".join(hits)
