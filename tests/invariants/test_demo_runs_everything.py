"""apps/demo loads cleanly and every role has golden tests. (End-to-end run added by the lead.)"""

from __future__ import annotations

from pathlib import Path

import yaml
from biz_kernel.loader import load_composition

ROOT = Path(__file__).resolve().parents[2]


def test_demo_loads_every_pack_present() -> None:
    m = load_composition(ROOT / "apps" / "demo", ROOT)
    on_disk_roles = {d.name for d in (ROOT / "roles").iterdir() if d.is_dir()}
    on_disk_caps = {
        d.name for d in (ROOT / "capabilities").iterdir() if (d / "capability.yaml").exists()
    }
    assert set(m.roles) == on_disk_roles
    assert set(m.capabilities) == on_disk_caps


def test_every_role_has_golden_tests() -> None:
    m = load_composition(ROOT / "apps" / "demo", ROOT)
    for role in m.roles.values():
        goldens = sorted((role.path / "tests" / "golden").glob("*.yaml"))
        assert goldens, f"{role.name} has no golden tests"
        for g in goldens:
            case = yaml.safe_load(g.read_text())
            assert {"name", "input", "expect"} <= set(case)


def test_hive_loads_too() -> None:
    load_composition(ROOT / "apps" / "hive", ROOT)
