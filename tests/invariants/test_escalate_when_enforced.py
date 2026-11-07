"""Every escalate_when a role declares is a condition the kernel enforces.

Roles declare conditions as data; the kernel's generic evaluator must parse every one
(no silent ignoring), each must be raisable, and a confidence condition must be able to
see a confidence in every draft the role makes (missing confidence fails closed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from biz_kernel.workflows.escalation import PENDING_SIGNALS, check, parse

ROOT = Path(__file__).resolve().parents[2]
ROLES = sorted(p.parent for p in (ROOT / "roles").glob("*/role.yaml"))


@pytest.mark.parametrize("role_dir", ROLES, ids=lambda p: p.name)
def test_every_declared_condition_is_enforced(role_dir: Path) -> None:
    role = yaml.safe_load((role_dir / "role.yaml").read_text())
    declared = role.get("escalate_when", [])
    conds = parse(declared, known_signals=PENDING_SIGNALS)
    assert len(conds) == len(declared)
    for c in conds:
        if c.name == "confidence_below":
            assert c.threshold is not None
            assert check((c,), kind="draft", output={"confidence": c.threshold - 0.01})
            assert check((c,), kind="draft", output={})  # fail closed
            assert check((c,), kind="draft", output={"confidence": 1.0}) is None
        else:
            assert c.name in PENDING_SIGNALS  # declared, not yet enforced (loader warns)


@pytest.mark.parametrize("role_dir", ROLES, ids=lambda p: p.name)
def test_confidence_roles_have_confidence_in_every_draft(role_dir: Path) -> None:
    role = yaml.safe_load((role_dir / "role.yaml").read_text())
    declared = role.get("escalate_when", [])
    if not any(isinstance(c, dict) and "confidence_below" in c for c in declared):
        return
    for topic in role["topics"]:
        sop = yaml.safe_load((role_dir / topic["sop"]).read_text())
        for step in sop["steps"]:
            if step["kind"] == "draft":
                required = step["schema"].get("required", [])
                assert "confidence" in required, (topic["sop"], step["id"])
