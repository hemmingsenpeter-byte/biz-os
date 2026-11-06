"""Role packs hold YAML and Markdown only: no Python."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = {".py", ".pyi", ".js", ".ts", ".sh", ".so"}


def test_roles_are_data() -> None:
    code = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "roles").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and (p.suffix in CODE or p.suffix == ".pyc")
    ]
    assert not code, code
