"""The loader refuses missing capabilities, unfilled slots and wrong versions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from biz_kernel.loader import LoadError, load_composition

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for d in ("packets", "roles", "industries", "capabilities", "apps/demo"):
        shutil.copytree(ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _edit(path: Path, fn: Callable[[dict], None]) -> None:
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _load(repo: Path) -> None:
    load_composition(repo / "apps" / "demo", repo)


def test_baseline_loads(repo: Path) -> None:
    _load(repo)


def test_missing_capability_refused(repo: Path) -> None:
    _edit(repo / "apps/demo/composition.yaml", lambda d: d.update(capabilities=[]))
    with pytest.raises(LoadError) as e:
        _load(repo)
    assert any("demo_desk" in p for p in e.value.problems)


def test_unfilled_slot_refused(repo: Path) -> None:
    _edit(
        repo / "industries/demo_trade/industry.yaml",
        lambda d: d["fills"].pop("industry.reply_template"),
    )
    with pytest.raises(LoadError) as e:
        _load(repo)
    assert any("industry.reply_template" in p for p in e.value.problems)


def test_wrong_kernel_version_refused(repo: Path) -> None:
    _edit(repo / "roles/demo_clerk/role.yaml", lambda d: d["requires"].update(kernel=">=2,<3"))
    with pytest.raises(LoadError) as e:
        _load(repo)
    assert any("needs kernel" in p for p in e.value.problems)


def test_all_problems_reported_together(repo: Path) -> None:
    _edit(repo / "apps/demo/composition.yaml", lambda d: d.update(capabilities=[], kernel="2.x"))
    _edit(repo / "industries/demo_trade/industry.yaml", lambda d: d["fills"].clear())
    with pytest.raises(LoadError) as e:
        _load(repo)
    assert len(e.value.problems) >= 3


def test_missing_packet_schema_refused(repo: Path) -> None:
    shutil.rmtree(repo / "packets/message.sent")
    with pytest.raises(LoadError) as e:
        _load(repo)
    assert any("message.sent" in p for p in e.value.problems)


def test_unknown_tool_and_goal_refused(repo: Path) -> None:
    _edit(
        repo / "roles/demo_clerk/role.yaml", lambda d: d["topics"][0]["tools"].append("nope.tool")
    )
    _edit(repo / "roles/demo_clerk/role.yaml", lambda d: d["goals"].append("no_such_goal"))
    with pytest.raises(LoadError) as e:
        _load(repo)
    text = str(e.value)
    assert "nope.tool" in text and "no_such_goal" in text
