"""A stale form may neither overwrite a correction nor append false training history."""

import pytest
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import Correction, StagedOperation
from biz_kernel.auth import tokens
from biz_kernel.auth.users import create_user
from biz_kernel.policy.types import Action, SideEffect
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


def test_edit_conflict_preserves_recipient_and_correction(client, session) -> None:
    user = create_user(session, "editor@example.invalid", "owner", "Owner")
    client.cookies.set("biz_session", tokens.issue_session(user.id))
    op = staged.stage(
        session,
        Action(
            actor="r",
            tool="mail.send",
            side_effect=SideEffect.EXTERNAL,
            inputs={"to": "wrong@example.invalid", "body": "original"},
        ),
        "owner",
    )
    first = client.post(
        f"/v1/approvals/{op.id}/edit",
        json={
            "version": 1,
            "inputs": {"to": "correct@example.invalid", "body": "original"},
            "reason": "wrong recipient",
            "severity": "policy",
        },
    )
    assert first.status_code == 200 and first.json()["version"] == 2
    second = client.post(
        f"/v1/approvals/{op.id}/edit",
        json={
            "version": 1,
            "inputs": {"to": "wrong@example.invalid", "body": "polite"},
            "reason": "tone",
            "severity": "cosmetic",
        },
    )
    assert second.status_code == 409
    assert session.get(StagedOperation, op.id).inputs["to"] == "correct@example.invalid"
    assert session.scalar(select(func.count()).select_from(Correction)) == 1
    assert client.post(f"/v1/approvals/{op.id}/edit", json={"inputs": {}}).status_code == 422


def test_locked_edit_refreshes_an_identity_map_snapshot(fresh_db: str) -> None:
    from biz_kernel import migrations

    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    with Session(engine) as first, first.begin():
        op = staged.stage(
            first,
            Action(
                actor="r", tool="mail.send", side_effect=SideEffect.EXTERNAL, inputs={"to": "wrong"}
            ),
            "owner",
        )
        identifier = op.id
    with Session(engine, expire_on_commit=False) as stale:
        snapshot = stale.get(StagedOperation, identifier)
        assert snapshot is not None and snapshot.version == 1
        with Session(engine) as fresh, fresh.begin():
            staged.edit(fresh, identifier, {"to": "correct"}, by="owner", version=1)
        with pytest.raises(staged.StaleVersion):
            staged.edit(stale, identifier, {"to": "wrong"}, by="owner", version=1)
        stale.rollback()
    engine.dispose()
