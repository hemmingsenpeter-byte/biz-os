"""`biz doctor` passes on the shipped apps and fails on a broken copy."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from biz_kernel.cli.main import app
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()


@pytest.mark.parametrize("name", ["demo", "hive"])
def test_doctor_passes(name: str) -> None:
    res = runner.invoke(app, ["doctor", str(ROOT / "apps" / name)])
    assert res.exit_code == 0, res.output


def _broken(tmp_path: Path, edit) -> Path:  # type: ignore[no-untyped-def]
    dest = tmp_path / "demo"
    shutil.copytree(ROOT / "apps" / "demo", dest)
    comp = dest / "composition.yaml"
    comp.write_text(edit(comp.read_text()))
    return dest


BREAKS = [
    lambda t: t.replace("fake_mail", "fake_nonexistent"),
    lambda t: t.replace("mail: fake_mail", "mail: no_such_port"),
    lambda t: t.replace(
        "capabilities: [demo_desk, intake, booking, comms]", "capabilities: [nope]"
    ),
]


@pytest.mark.parametrize("edit", BREAKS)
def test_doctor_fails_on_broken_copy(tmp_path: Path, edit) -> None:  # type: ignore[no-untyped-def]
    dest = _broken(tmp_path, edit)
    res = runner.invoke(app, ["doctor", str(dest), "--repo-root", str(ROOT)])
    assert res.exit_code == 1, res.output
    assert "✗" in res.output


def test_broken_copy_mutations_actually_apply():
    """Each mutation must change the composition, or the 'fails' test is vacuous."""
    text = (ROOT / "apps" / "demo" / "composition.yaml").read_text()
    for mutate in BREAKS:
        assert mutate(text) != text
