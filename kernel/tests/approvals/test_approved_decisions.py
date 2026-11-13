"""Owner decisions of 2026-11-13: cosmetic edits keep the promotion streak; edits name the
version they change."""

from __future__ import annotations

import pytest
from biz_kernel.approvals import autonomy, staged
from biz_kernel.approvals.models import AutonomyLevel
from biz_kernel.policy.types import Action, SideEffect
from sqlalchemy import select


def _op(session, decision_type="reply_sending"):
    action = Action(
        actor="r",
        tool="t.send",
        side_effect=SideEffect.EXTERNAL,
        inputs={"body": "draft", "idempotency_key": "k"},
        decision_type=decision_type,
    )
    return staged.stage(session, action, "owner")


def _streak(session, decision_type="reply_sending") -> int:
    row = session.scalar(
        select(AutonomyLevel).where(
            AutonomyLevel.role == "r", AutonomyLevel.decision_type == decision_type
        )
    )
    return row.approved_unchanged_streak if row is not None else 0


def _approve(session, op):
    staged.approve(session, op.id, "owner", session.get(type(op), op.id).version)


def test_cosmetic_edits_keep_the_streak(session):
    _approve(session, _op(session))  # unchanged: 1
    op = _op(session)
    staged.edit(session, op.id, {"body": "Draft."}, by="owner", severity="cosmetic")
    staged.edit(session, op.id, {"body": "Draft!"}, by="owner", severity="cosmetic")
    _approve(session, op)
    assert _streak(session) == 2


@pytest.mark.parametrize(
    "edits",
    [
        [("owner", "factual")],
        [("owner", "policy")],
        [("owner", None)],  # untagged counts as an error
        [("owner", "cosmetic"), ("owner", "factual")],
        [("r", None)],  # the role's own rewrite leaves no correction: not a clean approval
        [("", None)],  # unattributed edit: likewise
    ],
)
def test_other_edits_break_the_streak(session, edits):
    _approve(session, _op(session))
    assert _streak(session) == 1
    op = _op(session)
    for i, (by, severity) in enumerate(edits):
        staged.edit(session, op.id, {"body": f"edit {i}"}, by=by, severity=severity)
    _approve(session, op)
    assert _streak(session) == 0


def test_promotion_bar_counts_cosmetic_approvals(session):
    for i in range(3):
        op = _op(session)
        if i % 2:
            staged.edit(session, op.id, {"body": "tidy"}, by="owner", severity="cosmetic")
        _approve(session, op)
    assert autonomy.promotion_due(session, "r", "reply_sending", bar=3)


def test_edit_names_its_version(session):
    op = _op(session)
    staged.edit(session, op.id, {"body": "a"}, by="owner", severity="cosmetic", version=1)
    with pytest.raises(staged.StaleVersion):  # a second editor still looking at version 1
        staged.edit(session, op.id, {"body": "b"}, by="x@example.com", version=1)
    current = session.get(type(op), op.id)
    assert current.version == 2 and current.inputs["body"] == "a"
    staged.edit(session, op.id, {"body": "c"}, by="owner", version=2)
    assert session.get(type(op), op.id).version == 3
