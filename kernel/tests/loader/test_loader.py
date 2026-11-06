"""Loader unit tests against the demo app."""

from __future__ import annotations

from pathlib import Path

import pytest
from biz_kernel.cli.main import app
from biz_kernel.loader import LoadError, load_composition, parse_cadence
from biz_kernel.loader.loader import kernel_specifier
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def test_manifest_contents() -> None:
    m = load_composition(ROOT / "apps/demo", ROOT)
    role = m.roles["demo_clerk"]
    assert role.allow_list == [
        "demo_desk.read_request",
        "demo_desk.draft_reply",
        "demo_desk.send_reply",
    ]
    assert role.topics[0].sop["steps"][0]["id"] == "read"
    assert role.topics[0].decision_types == ["reply_drafting", "reply_sending"]
    assert m.role_budget("demo_clerk") == 10
    assert m.tools["demo_desk.send_reply"] == ("demo_desk", "external")
    assert "demo_clerk" in m.render()
    assert m.registry.latest("message.sent") == 1


def test_cadence_and_kernel_specs() -> None:
    assert parse_cadence("30m").total_seconds() == 1800
    with pytest.raises(ValueError):
        parse_cadence("soon")
    assert "1.0.0" in kernel_specifier("1.x")
    assert "2.0.0" not in kernel_specifier("1.x")


def test_missing_app_raises() -> None:
    with pytest.raises(LoadError):
        load_composition(ROOT / "apps/nope", ROOT)


def test_cli_load_and_doctor() -> None:
    r = CliRunner()
    assert r.invoke(app, ["load", str(ROOT / "apps/demo")]).exit_code == 0
    out = r.invoke(app, ["doctor", str(ROOT / "apps/demo")])
    assert out.exit_code == 0 and "✓" in out.output
    bad = r.invoke(app, ["doctor", str(ROOT / "apps/nope")])
    assert bad.exit_code == 1 and "✗" in bad.output
