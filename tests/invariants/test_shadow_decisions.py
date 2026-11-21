"""Owner decisions on shadow mode.

1. Suppressing an approved operation is terminal: it leaves the queue and never runs.
2. Leaving shadow needs the owner's recorded authorization; the env override only tightens.
3. Suppressions record the verdict policy would have given, computed without side effects.
Also: shadow refuses a cloud model unless the policy explicitly allows it.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import typer
import yaml
from biz_kernel.api.app import create_app
from biz_kernel.approvals import staged
from biz_kernel.approvals.models import AutonomyLevel, StagedOperation
from biz_kernel.audit.log import AuditEntry
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.cli.main import doctor_cmd
from biz_kernel.policy import spend
from biz_kernel.policy.engine import PolicyEngine
from biz_kernel.policy.models import MoneySpend
from biz_kernel.policy.types import Action, SideEffect
from biz_kernel.runtime import CompositionError, build_runtime
from biz_kernel.tools.registry import Tool, ToolRegistry
from biz_kernel.tools.shadow import SHADOW_EVIDENCE, ExecutionMode
from biz_port_fakes import FakeTelephony
from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
SEND = "intake.text_back"
ARGS = {"to": "+64210000001", "idempotency_key": "shadow-terminal"}
HIVE_SLOTS = ("mail", "calendar", "telephony", "model_local")
AUTHORIZATION = {
    "authorized_by": "fixture-owner",
    "authorized_on": "2026-11-21",
    "pilot_exit_evidence": "fixture: fake ports only",
}


def _suppressions(session: Session, subject: str) -> list[AuditEntry]:
    return list(
        session.scalars(
            select(AuditEntry).where(
                AuditEntry.kind == SHADOW_EVIDENCE, AuditEntry.subject == subject
            )
        )
    )


def _hive_copy(
    tmp_path: Path, composition: dict[str, Any] | None = None, policy: Any = None
) -> Path:
    target = Path(shutil.copytree(ROOT / "apps/hive", tmp_path / "hive-copy"))
    config = target / "composition.yaml"
    data = yaml.safe_load(config.read_text())
    for key, value in (composition or {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    config.write_text(yaml.safe_dump(data))
    if policy is not None:
        path = target / "policy.yaml"
        path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), **policy}))
    return target


def _slots() -> dict[str, object]:
    return {key: object() for key in HIVE_SLOTS}


@pytest.fixture
def shadow_demo(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shadow")
    phone = FakeTelephony()
    send = AsyncMock(wraps=phone.send_sms)
    monkeypatch.setattr(phone, "send_sms", send)
    rt = build_runtime(ROOT / "apps/demo", ROOT, ports={"telephony": phone})
    assert rt.registry.execution_mode == ExecutionMode.SHADOW
    return rt, send


def _approved(session: Session) -> StagedOperation:
    op = staged.stage(session, Action("receptionist", SEND, SideEffect.EXTERNAL, ARGS), "owner")
    return staged.approve(session, op.id, "owner", version=1)


# ------------------------------------------------------------- 1. terminal suppression


def test_suppressed_operation_is_terminal_even_after_leaving_shadow(session, shadow_demo):
    rt, send = shadow_demo
    op = _approved(session)
    assert op in staged.pending(session)

    first = staged.execute(session, op.id, rt.registry, rt.policy)
    assert not first.ran and first.suppression is not None
    assert op.status == staged.SHADOWED and op.executed_at is None
    assert op not in staged.pending(session)

    again = staged.execute(session, op.id, rt.registry, rt.policy)
    assert again.suppression == first.suppression and not again.ran

    rt.registry.execution_mode = ExecutionMode.SUPERVISED  # the mode later loosens
    later = staged.execute(session, op.id, rt.registry, rt.policy)
    assert not later.ran and later.suppression == first.suppression
    assert op.status == staged.SHADOWED

    assert len(_suppressions(session, SEND)) == 1
    events = session.scalars(
        select(AuditEntry.detail["event"].astext).where(
            AuditEntry.kind == "approval", AuditEntry.subject == str(op.id)
        )
    ).all()
    assert events.count(staged.SHADOWED) == 1
    send.assert_not_called()


def test_shadowed_operation_cannot_be_revived(session, shadow_demo):
    rt, _ = shadow_demo
    op = _approved(session)
    staged.execute(session, op.id, rt.registry, rt.policy)
    with pytest.raises(staged.ApprovalError):
        staged.approve(session, op.id, "owner", version=op.version)
    with pytest.raises(staged.ApprovalError):
        staged.edit(session, op.id, {**ARGS, "to": "+64210000009"}, version=op.version)
    with pytest.raises(staged.ApprovalError):
        staged.reject(session, op.id, "owner")
    assert op.status == staged.SHADOWED


def test_suppression_and_terminal_status_commit_together(session, shadow_demo):
    """A crash before commit leaves neither, so a resumed step suppresses exactly once."""
    rt, send = shadow_demo
    op = _approved(session)
    with pytest.raises(RuntimeError), session.begin_nested():
        staged.execute(session, op.id, rt.registry, rt.policy)
        raise RuntimeError("crash before commit")
    session.expire_all()
    assert session.get(StagedOperation, op.id).status == "approved"
    assert _suppressions(session, SEND) == []

    resumed = staged.execute(session, op.id, rt.registry, rt.policy)
    assert resumed.suppression is not None
    assert session.get(StagedOperation, op.id).status == staged.SHADOWED
    assert len(_suppressions(session, SEND)) == 1
    send.assert_not_called()


# ------------------------------------------------------------- 2. owner authorization


def test_env_override_cannot_loosen_shadow(monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "supervised")
    resolve = Mock(side_effect=AssertionError("must refuse before building ports"))
    monkeypatch.setattr("biz_kernel.runtime.resolve_port", resolve)
    with pytest.raises(CompositionError, match="cannot loosen the composition's shadow mode"):
        build_runtime(ROOT / "apps/hive", ROOT)
    resolve.assert_not_called()


def test_env_override_may_tighten(monkeypatch):
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "shadow")
    assert build_runtime(ROOT / "apps/demo", ROOT).registry.execution_mode == ExecutionMode.SHADOW


@pytest.mark.parametrize(
    ("authorization", "problem"),
    [
        (None, "supervised mode needs supervised_authorization"),
        ({**AUTHORIZATION, "pilot_exit_evidence": " "}, "pilot_exit_evidence must be set"),
        ({**AUTHORIZATION, "authorized_by": ""}, "authorized_by must be set"),
        ({**AUTHORIZATION, "authorized_on": "soon"}, "authorized_on must be a date"),
        ({**AUTHORIZATION, "authorized_on": "2999-01-01"}, "cannot be in the future"),
    ],
)
def test_supervised_needs_a_complete_owner_authorization(
    tmp_path, monkeypatch, capsys, authorization, problem
):
    monkeypatch.delenv("BIZ_EXECUTION_MODE", raising=False)
    app = _hive_copy(
        tmp_path,
        {"execution_mode": "supervised", "supervised_authorization": authorization},
    )
    if authorization is not None and not all(authorization.values()):
        # the loader schema rejects empty strings before the runtime sees them
        with pytest.raises(Exception, match="supervised_authorization|cannot start"):
            build_runtime(app, ROOT, ports=_slots())
        return
    with pytest.raises(CompositionError, match=problem):
        build_runtime(app, ROOT, ports=_slots())
    with pytest.raises(typer.Exit):
        doctor_cmd(app, ROOT)
    assert "✗ execution mode supervised\n  " in capsys.readouterr().out


def test_recorded_authorization_allows_supervised(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BIZ_EXECUTION_MODE", raising=False)
    app = _hive_copy(
        tmp_path, {"execution_mode": "supervised", "supervised_authorization": AUTHORIZATION}
    )
    rt = build_runtime(app, ROOT, ports=_slots())
    assert rt.registry.execution_mode == ExecutionMode.SUPERVISED
    assert create_app(rt.manifest).state.execution_mode == ExecutionMode.SUPERVISED
    doctor_cmd(app, ROOT)
    assert "✓ execution mode supervised, authorized by fixture-owner" in capsys.readouterr().out


def test_absent_mode_fails_closed_to_shadow(tmp_path, monkeypatch):
    monkeypatch.delenv("BIZ_EXECUTION_MODE", raising=False)
    app = _hive_copy(tmp_path, {"execution_mode": None})
    assert build_runtime(app, ROOT, ports=_slots()).registry.execution_mode == ExecutionMode.SHADOW
    monkeypatch.setenv("BIZ_EXECUTION_MODE", "supervised")
    with pytest.raises(CompositionError, match="cannot loosen"):
        build_runtime(app, ROOT, ports=_slots())


def test_api_and_doctor_apply_the_same_rule(monkeypatch, capsys):
    from biz_kernel.loader import load_composition

    monkeypatch.setenv("BIZ_EXECUTION_MODE", "supervised")
    with pytest.raises(ValueError, match="cannot loosen"):
        create_app(load_composition(ROOT / "apps/hive", ROOT))
    with pytest.raises(typer.Exit):
        doctor_cmd(ROOT / "apps/hive", ROOT)
    assert "✗ execution mode" in capsys.readouterr().out
    monkeypatch.delenv("BIZ_EXECUTION_MODE")
    doctor_cmd(ROOT / "apps/hive", ROOT)
    assert "✓ execution mode shadow" in capsys.readouterr().out


def test_hive_records_no_authorization():
    composition = yaml.safe_load((ROOT / "apps/hive/composition.yaml").read_text())
    assert composition["execution_mode"] == "shadow"
    assert "supervised_authorization" not in composition


# ------------------------------------------------------------- cloud model in shadow


@pytest.mark.parametrize(
    "ports",
    [{"model_cloud": "fake_model"}, {"model_local": "model_claude"}],
    ids=["cloud-slot", "cloud-adapter"],
)
def test_shadow_refuses_a_cloud_model_unless_policy_allows(tmp_path, monkeypatch, capsys, ports):
    monkeypatch.delenv("BIZ_EXECUTION_MODE", raising=False)
    composition = yaml.safe_load((ROOT / "apps/hive/composition.yaml").read_text())
    app = _hive_copy(tmp_path, {"ports": {**composition["ports"], **ports}})
    slot = next(iter(ports))
    with pytest.raises(CompositionError, match=f"port {slot} is a cloud model"):
        build_runtime(app, ROOT, ports=_slots())
    with pytest.raises(typer.Exit):
        doctor_cmd(app, ROOT)
    assert f"port {slot} is a cloud model" in capsys.readouterr().out

    shutil.rmtree(app)
    allowed = _hive_copy(
        tmp_path,
        {"ports": {**composition["ports"], **ports}},
        policy={"shadow": {"allow_cloud_model": True}},
    )
    rt = build_runtime(allowed, ROOT, ports={**_slots(), slot: object()})
    assert rt.registry.execution_mode == ExecutionMode.SHADOW


def test_shadow_counts_an_overridden_cloud_slot(monkeypatch):
    monkeypatch.delenv("BIZ_EXECUTION_MODE", raising=False)
    with pytest.raises(CompositionError, match="port model_cloud is a cloud model"):
        build_runtime(ROOT / "apps/hive", ROOT, ports={**_slots(), "model_cloud": object()})


# ------------------------------------------------------------- 3. would-be verdict


def _verdict_stack() -> tuple[ToolRegistry, PolicyEngine, Mock]:
    registry = ToolRegistry(execution_mode=ExecutionMode.SHADOW)
    provider = Mock()
    schema = {"type": "object"}
    for name, effect in (
        ("send", SideEffect.EXTERNAL),
        ("blocked", SideEffect.EXTERNAL),
        ("pay", SideEffect.MONEY),
    ):
        registry.register(Tool(name, "test", effect, schema, provider, cost_usd=1))
    registry.budgets = BudgetBook({"worker": 10})
    policy = PolicyEngine(
        {
            "autonomy": {"default": 2, "pay": 4},
            "hard_rules": {"deny_tools": ["blocked"]},
            "limits": {"max_amount": {"default": 100}, "max_daily": {"default": 1000}},
        },
        registry,
    )
    return registry, policy, provider


def test_suppression_records_the_would_be_verdict_without_side_effects(session, monkeypatch):
    registry, policy, provider = _verdict_stack()
    lock = Mock(side_effect=AssertionError("a pure evaluation takes no spend lock"))
    monkeypatch.setattr(spend, "lock", lock)
    charge = Mock(side_effect=AssertionError("no budget is charged"))
    monkeypatch.setattr(registry.budgets, "charge", charge)
    expected = {
        "send": {"decision": "ask", "reason": "autonomy level 2 requires approval"},
        "blocked": {"decision": "deny", "reason": "hard rule: blocked is denied"},
        "pay": {"decision": "allow", "reason": "autonomy level 4"},
    }
    for name, would_be in expected.items():
        args = {"to": "fixed@example.invalid", "amount": 50, "idempotency_key": name}
        result = registry.call(session, policy, actor="worker", name=name, args=args)
        assert not result.ran and result.suppression is not None
        audit = session.get(AuditEntry, result.suppression.audit_id)
        assert audit.detail["policy"] == would_be
        assert audit.detail["pre_approved"] is False

    provider.assert_not_called()
    lock.assert_not_called()
    charge.assert_not_called()
    assert session.scalar(select(func.count()).select_from(MoneySpend)) == 0
    assert session.scalar(select(func.count()).select_from(StagedOperation)) == 0
    assert session.scalar(select(func.count()).select_from(AutonomyLevel)) == 0
    assert session.scalar(select(func.count()).where(AuditEntry.kind == "policy")) == 0


def test_evaluate_answers_what_check_would(session):
    registry, policy, _ = _verdict_stack()
    for name in ("send", "blocked", "pay"):
        for amount in (50, 500):
            action = registry.action("worker", name, {"amount": amount})
            assert policy.evaluate(session, action) == policy.check(session, action)


def test_staged_suppression_records_verdict_and_operation(session, shadow_demo):
    rt, _ = shadow_demo
    op = _approved(session)
    result = staged.execute(session, op.id, rt.registry, rt.policy)
    assert result.suppression is not None
    detail = session.get(AuditEntry, result.suppression.audit_id).detail
    action = rt.registry.action(op.actor, op.tool, op.inputs, op.decision_type)
    expected = rt.policy.evaluate(session, action)
    assert detail["policy"]["reason"] == expected.reason
    assert detail["policy"]["decision"] in {"allow", "ask", "deny"}
    assert detail["pre_approved"] is True and detail["staged_op_id"] == str(op.id)


def test_checker_without_pure_evaluation_is_never_called(session):
    registry, _, provider = _verdict_stack()
    checker = Mock(spec=["check"])
    checker.check.side_effect = AssertionError("check may have side effects")
    result = registry.call(session, checker, actor="worker", name="send", args={})
    detail = session.get(AuditEntry, result.suppression.audit_id).detail
    assert detail["policy"]["decision"] == "unknown"
    checker.check.assert_not_called()
    provider.assert_not_called()
    assert uuid.UUID(str(result.suppression.audit_id))


def test_datetime_authorization_date_is_accepted_not_crashing():
    from datetime import datetime

    from biz_kernel.tools.shadow import authorization_problems

    authorization = {
        "authorized_by": "owner",
        "authorized_on": datetime(2026, 11, 1, 9, 0),
        "pilot_exit_evidence": "pilot report",
    }
    assert authorization_problems(authorization) == []
    authorization["authorized_on"] = datetime(2999, 1, 1, 9, 0)
    assert any("future" in p for p in authorization_problems(authorization))
