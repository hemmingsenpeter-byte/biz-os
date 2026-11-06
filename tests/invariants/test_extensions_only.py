"""Pack migrations create only ext_<prefix>_ tables and never touch kernel tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from biz_kernel.loader import (
    IndustryPack,
    LoadError,
    check_extensions,
    install_pack,
    load_composition,
    uninstall_pack,
)
from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[2]


def _pack(prefix: str = "fitout", name: str = "commercial_pack") -> IndustryPack:
    return IndustryPack(name, "1.0.0", ROOT, prefix, [], {}, {})


def test_prefix_enforced() -> None:
    check_extensions(_pack(), ["ext_fitout_job", "ext_fitout_site"])
    with pytest.raises(LoadError):
        check_extensions(_pack(), ["ext_fitout_job", "jobs"])
    with pytest.raises(LoadError):
        check_extensions(_pack(), ["ext_other_job"])


def test_default_prefix_is_pack_name() -> None:
    pack = IndustryPack("plain", "1.0.0", ROOT, "plain", [], {}, {})
    assert pack.table_prefix == "ext_plain_"


def test_install_creates_only_ext_tables_and_uninstall_drops_them(engine) -> None:  # type: ignore[no-untyped-def]
    pack = load_composition(ROOT / "apps" / "demo", ROOT).industries["demo_trade"]
    before = set(inspect(engine).get_table_names())
    created = install_pack(engine, pack)
    after = set(inspect(engine).get_table_names())
    assert created and all(t.startswith("ext_demo_") for t in created)
    assert after - before == set(created)
    uninstall_pack(engine, pack)
    assert set(inspect(engine).get_table_names()) == before
