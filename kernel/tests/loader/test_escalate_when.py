"""The loader and `biz doctor` reject unknown or unenforceable escalate_when conditions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from biz_kernel.cli.main import app
from biz_kernel.loader import LoadError, load_composition
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for d in ("packets", "roles", "industries", "capabilities", "apps/demo"):
        shutil.copytree(ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _edit(path: Path, fn: Callable[[dict], None]) -> None:  # type: ignore[type-arg]
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _problems(repo: Path) -> str:
    with pytest.raises(LoadError) as e:
        load_composition(repo / "apps" / "demo", repo)
    return "\n".join(e.value.problems)


@pytest.mark.parametrize("bad", ["no_such_condition", {"confidence_below": "x"}, {"nope": 1}])
def test_unknown_condition_rejected(repo: Path, bad: object) -> None:
    _edit(
        repo / "roles/demo_clerk/role.yaml",
        lambda d: d.update(escalate_when=[*d["escalate_when"], bad]),
    )
    assert "escalate_when" in _problems(repo)


def test_doctor_rejects_unknown_condition(repo: Path) -> None:
    _edit(
        repo / "roles/demo_clerk/role.yaml",
        lambda d: d.update(escalate_when=["no_such_condition"]),
    )
    res = CliRunner().invoke(app, ["doctor", str(repo / "apps/demo"), "--repo-root", str(repo)])
    assert res.exit_code == 1 and "escalate_when" in res.output


def test_confidence_condition_needs_confidence_in_every_draft(repo: Path) -> None:
    def drop(d: dict) -> None:  # type: ignore[type-arg]
        for step in d["steps"]:
            if step["kind"] == "draft":
                step["schema"]["required"] = [
                    r for r in step["schema"]["required"] if r != "confidence"
                ]

    _edit(repo / "roles/demo_clerk/sops/handle_request.yaml", drop)
    assert "confidence" in _problems(repo)
