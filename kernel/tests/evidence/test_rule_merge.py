"""Declared evidence rules from several roles never overwrite each other."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from biz_kernel.evidence.service import merge_rules
from biz_kernel.loader import LoadError, load_composition


def test_conflicting_rules_merge_to_the_strictest():
    rules, conflicts = merge_rules(
        {"quote": ["approval.granted"], "x": ["a"]},
        {"quote": ["packet:estimate.ready"], "x": ["a"]},
    )
    assert set(rules["quote"]) == {"approval.granted", "packet:estimate.ready"}
    assert rules["x"] == ["a"]
    assert conflicts == ["quote"]


def test_later_weaker_rule_does_not_loosen_earlier():
    rules, _ = merge_rules({"k": ["a", "b"]}, {"k": ["a"]})
    assert set(rules["k"]) == {"a", "b"}


def test_loader_rejects_conflicting_role_declarations(tmp_path: Path):
    root = Path(__file__).resolve().parents[3]
    work = tmp_path / "repo"
    shutil.copytree(
        root, work, ignore=shutil.ignore_patterns(".venv", ".git", "node_modules", "web")
    )
    est = work / "roles" / "estimator" / "role.yaml"
    est.write_text(est.read_text().replace("  pricing:", '  triage: ["x.other"]\n  pricing:', 1))
    # estimator now also declares `triage`, which the receptionist declares differently.
    with pytest.raises(LoadError, match="triage"):
        load_composition(work / "apps" / "hive", work)
