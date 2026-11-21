"""Shadow execution retains internal artifacts and denies provider writes."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import biz_cap_comms.models  # noqa: F401
import biz_cap_intake.models  # noqa: F401
import pytest
from biz_kernel import migrations
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged
from biz_kernel.audit.log import AuditEntry
from biz_kernel.auth.deps import get_session
from biz_kernel.auth.users import create_user
from biz_kernel.control import service as control
from biz_kernel.policy.types import Action, Allow, SideEffect
from biz_kernel.runtime import build_runtime
from biz_kernel.tools.registry import Tool, ToolRegistry
from biz_kernel.tools.shadow import SHADOW_EVIDENCE, ExecutionMode
from biz_port_fakes import FakeMail, FakeTelephony
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
SEND = "intake.text_back"
ARGS = {"to": "+64210000001", "idempotency_key": "shadow-proof"}


class AllowEverything:
    def check(self, session: Session, action: Action) -> Allow:
        return Allow()


def test_shadow_env_preempts_even_preapproved_dispatch(session, monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shadow")
    phone = FakeTelephony()
    send = AsyncMock(wraps=phone.send_sms)
    monkeypatch.setattr(phone, "send_sms", send)
    rt = build_runtime(ROOT / "apps/demo", ROOT, ports={"telephony": phone})
    result = rt.registry.call(
        session,
        rt.policy,
        actor="receptionist",
        name=SEND,
        args=ARGS,
        pre_approved=True,
    )
    assert not result.ran
    send.assert_not_called()
    assert result.suppression is not None
    audit = session.get(AuditEntry, result.suppression.audit_id)
    assert audit.kind == SHADOW_EVIDENCE and audit.subject == SEND
    assert audit.detail["recipients"] == [ARGS["to"]]


def test_mode_validation_precedes_ports_and_hive_defaults_to_shadow(monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shdaow")
    resolve = Mock(side_effect=AssertionError("must validate before building ports"))
    monkeypatch.setattr("biz_kernel.runtime.resolve_port", resolve)
    with pytest.raises(ValueError, match="BIZ_EXECUTION_MODE"):
        build_runtime(ROOT / "apps/hive", ROOT)
    resolve.assert_not_called()

    monkeypatch.delenv("BIZ_EXECUTION_MODE")
    slots = {key: object() for key in ("mail", "calendar", "telephony", "model_local")}
    rt = build_runtime(ROOT / "apps/hive", ROOT, ports=slots)
    assert rt.registry.execution_mode == ExecutionMode.SHADOW
    assert set(rt.manifest.roles) == {"receptionist"}
    assert set(rt.manifest.ports) == set(slots)
    assert rt.router.cloud is None


def test_mixed_nested_effects_and_unknown_graph_never_enter_wrapper(session):
    registry = ToolRegistry(execution_mode=ExecutionMode.SHADOW)
    provider = Mock()
    schema = {"type": "object"}
    for name, effect, inner in (
        ("send", SideEffect.EXTERNAL, ()),
        ("pay", SideEffect.MONEY, ()),
        ("bundle", SideEffect.READ, ("send", "pay")),
        ("outer", SideEffect.DRAFT, ("bundle",)),
        ("missing", SideEffect.DRAFT, ("absent",)),
        ("cycle", SideEffect.DRAFT, ("cycle",)),
    ):
        registry.register(Tool(name, "test", effect, schema, provider, inner=inner))

    for name, effects in (
        ("send", ["external"]),
        ("pay", ["money"]),
        ("outer", ["external", "money"]),
        ("missing", ["external"]),
        ("cycle", ["external"]),
    ):
        result = registry.call(
            session,
            AllowEverything(),
            actor="worker",
            name=name,
            args={"to": "fixed@example.invalid", "body": "private"},
            pre_approved=True,
        )
        assert result.suppression is not None and not result.ran
        audit = session.get(AuditEntry, result.suppression.audit_id)
        assert audit.detail["effects"] == effects
        assert audit.detail["recipients"] == ["fixed@example.invalid"]
        assert "private" not in str(audit.detail)
    provider.assert_not_called()


def test_stale_and_then_reapproved_operation_never_dispatches(session, monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shadow")
    phone = FakeTelephony()
    send = AsyncMock(wraps=phone.send_sms)
    monkeypatch.setattr(phone, "send_sms", send)
    rt = build_runtime(ROOT / "apps/demo", ROOT, ports={"telephony": phone})
    op = staged.stage(session, Action("receptionist", SEND, SideEffect.EXTERNAL, ARGS), "owner")
    staged.approve(session, op.id, "owner", version=1)
    staged.edit(session, op.id, {**ARGS, "to": "+64210000002"}, version=1)
    with pytest.raises(staged.NotApproved):
        staged.execute(session, op.id, rt.registry, rt.policy)
    staged.approve(session, op.id, "owner", version=2)

    first = staged.execute(session, op.id, rt.registry, rt.policy)
    for _ in range(2):
        result = staged.execute(session, op.id, rt.registry, rt.policy)
        assert not result.ran and result.suppression == first.suppression
        audit = session.get(AuditEntry, result.suppression.audit_id)
        assert audit.detail["recipients"] == ["+64210000002"]
        assert op.status == staged.SHADOWED and op.executed_at is None
    send.assert_not_called()


def test_shadow_blocks_direct_staff_magic_link_api(session, monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shadow")
    mail = FakeMail()
    send = AsyncMock(wraps=mail.send)
    monkeypatch.setattr(mail, "send", send)
    app = create_app(mail=mail)
    app.dependency_overrides[get_session] = lambda: session
    create_user(session, "staff@example.invalid", "staff", "Staff")
    with TestClient(app) as client:
        staff = client.post("/v1/auth/magic/request", json={"email": "staff@example.invalid"})
        unknown = client.post("/v1/auth/magic/request", json={"email": "other@example.invalid"})
    assert staff.status_code == unknown.status_code == 202
    assert staff.json() == unknown.json() == {"status": "accepted"}
    send.assert_not_called()
    evidence = session.scalars(select(AuditEntry).where(AuditEntry.kind == SHADOW_EVIDENCE)).one()
    assert evidence.subject == "auth.magic_link"
    assert evidence.detail["recipients"] == ["staff@example.invalid"]


def test_shadow_send_stays_blocked_when_pause_commits_mid_dispatch(fresh_db, monkeypatch):
    migrations.upgrade(fresh_db)
    engine = create_engine(fresh_db)
    registry = ToolRegistry(execution_mode=ExecutionMode.SHADOW)
    provider = Mock()
    registry.register(Tool("send", "test", SideEffect.EXTERNAL, {"type": "object"}, provider))
    entered, release = Event(), Event()
    real_suppression = registry.suppress_in_shadow

    def after_first_pause_check(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_suppression(*args, **kwargs)

    monkeypatch.setattr(registry, "suppress_in_shadow", after_first_pause_check)

    def sender():
        with Session(engine) as session, session.begin():
            return registry.call(session, AllowEverything(), actor="worker", name="send", args={})

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(sender)
            try:
                assert entered.wait(10)
                with Session(engine) as session, session.begin():
                    assert control.pause(session, "owner").paused
            finally:
                release.set()
            with pytest.raises(control.Paused):
                pending.result(timeout=10)

        with Session(engine) as session, session.begin():
            assert not control.resume(session, "owner").paused
        assert sender().suppression is not None
        provider.assert_not_called()
    finally:
        engine.dispose()
