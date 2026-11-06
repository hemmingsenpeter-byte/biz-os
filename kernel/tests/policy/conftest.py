"""Reuse the shared Postgres fixtures from tests/conftest.py (not on kernel/tests' path)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[3] / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_shared_conftest", _path)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

pg_server = _mod.pg_server
pg_url = _mod.pg_url
engine = _mod.engine
session = _mod.session
