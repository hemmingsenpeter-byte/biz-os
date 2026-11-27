"""Changed or unknown SOP definitions cannot resume recorded workflow steps."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from biz_kernel.db import utcnow
from biz_kernel.workflows import runner
from biz_kernel.workflows.models import SopRun, SopStep
from biz_kernel.workflows.versions import (
    WorkflowCompatibilityError,
    assert_startup_compatible,
    definition_sha256,
)
from sqlalchemy.orm import Session


def _spec(tmp_path: Path) -> runner.RoleSpec:
    (tmp_path / "draft.md").write_text("Original prompt.")
    return runner.RoleSpec(
        "fixture",
        {"fixture.render"},
        {"sample": {"steps": [{"id": "draft", "kind": "draft", "prompt": "draft.md"}]}},
        tmp_path,
    )


def test_changed_step_kind_refuses_before_old_raw_ledger_is_used(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    run = SopRun(
        id="versioned",
        role="fixture",
        sop="sample",
        status="waiting",
        definition_sha256=definition_sha256(spec, "sample"),
    )
    session.add(run)
    session.flush()
    session.add(SopStep(run_id=run.id, step_id="draft", output={"body": "Old raw draft."}))
    session.flush()
    assert_startup_compatible(session, {"fixture": spec})
    spec.sops["sample"]["steps"][0] = {
        "id": "draft",
        "kind": "tool",
        "tool": "fixture.render",
    }
    with pytest.raises(WorkflowCompatibilityError, match="differs"):
        assert_startup_compatible(session, {"fixture": spec})

    @contextmanager
    def scope():  # type: ignore[no-untyped-def]
        yield session

    monkeypatch.setattr(runner.db, "session_scope", scope)
    monkeypatch.setattr(runner, "ctx", lambda: type("Context", (), {"roles": {"fixture": spec}})())
    begin = Mock(side_effect=AssertionError("no checkpointed step may run"))
    monkeypatch.setattr(runner, "_begin", begin)
    with pytest.raises(WorkflowCompatibilityError, match="differs"):
        runner._run(run.id, "fixture", "sample", "unused")
    begin.assert_not_called()
    assert run.status == "waiting"
    step = session.get(SopStep, (run.id, "draft"))
    assert step is not None and step.output == {"body": "Old raw draft."}


def test_prompt_only_change_requires_drain(session: Session, tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    session.add(
        SopRun(
            id="prompt-version",
            role="fixture",
            sop="sample",
            status="paused",
            definition_sha256=definition_sha256(spec, "sample"),
        )
    )
    session.flush()
    (tmp_path / "draft.md").write_text("Changed prompt.")
    with pytest.raises(WorkflowCompatibilityError, match="original release"):
        assert_startup_compatible(session, {"fixture": spec})


def test_legacy_unfinished_refused_completed_work_is_not_replayed(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    run = SopRun(id="legacy", role="fixture", sop="sample", status="waiting")
    session.add(run)
    session.flush()
    with pytest.raises(WorkflowCompatibilityError, match="no pinned"):
        assert_startup_compatible(session, {"fixture": spec})
    run.status, run.finished_at = "done", utcnow()
    session.flush()
    assert_startup_compatible(session, {})

    @contextmanager
    def scope():  # type: ignore[no-untyped-def]
        yield session

    monkeypatch.setattr(runner.db, "session_scope", scope)
    begin = Mock(side_effect=AssertionError("completed legacy work must not run new steps"))
    monkeypatch.setattr(runner, "_begin", begin)
    assert runner._run(run.id, "fixture", "sample", "unused") == {
        "run_id": run.id,
        "status": "done",
    }
    begin.assert_not_called()


def test_inactive_role_refuses_pending_work(session: Session) -> None:
    session.add(SopRun(id="inactive", role="removed", sop="sample", status="budget_stopped"))
    session.flush()
    with pytest.raises(WorkflowCompatibilityError, match="no longer active"):
        assert_startup_compatible(session, {})
