"""`biz metrics` prints the report as a table or JSON; `biz doctor` checks the baseline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from biz_kernel.cli.main import app
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()


@pytest.fixture
def migrated(fresh_db: str) -> str:
    res = runner.invoke(app, ["migrate", str(ROOT / "apps" / "demo"), "--url", fresh_db])
    assert res.exit_code == 0, res.output
    return fresh_db


def test_metrics_json(migrated: str) -> None:
    res = runner.invoke(
        app, ["metrics", str(ROOT / "apps" / "demo"), "--url", migrated, "--days", "7", "--json"]
    )
    assert res.exit_code == 0, res.output
    body = json.loads(res.output)
    assert body["decision_types"] == [] and body["tasks_completed"] == 0
    assert body["cost_per_task_usd"] is None
    assert body["time_saved_minutes"] == 0.0  # the demo app has a baseline


def test_metrics_table_says_not_measured_without_a_baseline(migrated: str) -> None:
    res = runner.invoke(app, ["metrics", str(ROOT / "apps" / "hive"), "--url", migrated])
    assert res.exit_code == 0, res.output
    assert "Time saved:       not measured" in res.output
    assert "Cost per task:    not measured" in res.output
    assert "No proposals in this period." in res.output


def test_metrics_rejects_days_out_of_range() -> None:
    res = runner.invoke(app, ["metrics", str(ROOT / "apps" / "demo"), "--days", "0"])
    assert res.exit_code == 2


def test_doctor_reports_the_baseline() -> None:
    demo = runner.invoke(app, ["doctor", str(ROOT / "apps" / "demo")])
    assert "✓ time-saved baseline is usable" in demo.output
    hive = runner.invoke(app, ["doctor", str(ROOT / "apps" / "hive")])
    assert "✓ time-saved baseline not set (optional)" in hive.output


def test_doctor_fails_on_an_unusable_baseline(tmp_path: Path) -> None:
    dest = tmp_path / "demo"
    shutil.copytree(ROOT / "apps" / "demo", dest)
    policy = dest / "policy.yaml"
    text = policy.read_text()
    assert "default: 12" in text
    policy.write_text(text.replace("default: 12", "default: about twelve"))
    res = runner.invoke(app, ["doctor", str(dest), "--repo-root", str(ROOT)])
    assert res.exit_code == 1, res.output
    assert "✗ time-saved baseline" in res.output and "about twelve" in res.output
