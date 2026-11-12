"""The measurement report: buckets, window edges, honest nulls, cost and time saved."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from biz_kernel import evidence
from biz_kernel.approvals import autonomy
from biz_kernel.approvals.models import Correction, StagedOperation
from biz_kernel.audit.log import AuditEntry
from biz_kernel.budgets.models import CostEvent
from biz_kernel.metrics import config_problems, render, report, window
from biz_kernel.records.models import Task
from biz_kernel.workflows.models import SopRun
from sqlalchemy import func, select
from sqlalchemy.orm import Session

SINCE = datetime(2030, 3, 1, tzinfo=UTC)
UNTIL = datetime(2030, 3, 29, tzinfo=UTC)
MID = datetime(2030, 3, 10, tzinfo=UTC)
ROLE, KIND = "role_a", "reply"
BASELINE = {"baseline_minutes": {"default": 10, "long": 30}, "review_minutes_per_approval": 2}


def op(
    session: Session,
    status: str,
    *severities: str | None,
    at: datetime = MID,
    decision_type: str | None = KIND,
    role: str = ROLE,
) -> StagedOperation:
    row = StagedOperation(
        created_at=at,
        actor=role,
        tool="t.send",
        inputs={},
        inputs_sha256="0" * 64,
        approver="owner",
        status=status,
        decision_type=decision_type,
    )
    session.add(row)
    session.flush()
    for n, severity in enumerate(severities, start=1):
        session.add(
            Correction(
                op_id=row.id,
                actor=role,
                tool="t.send",
                decision_type=decision_type,
                from_version=n,
                to_version=n + 1,
                before={},
                after={},
                by="owner",
                severity=severity,
            )
        )
    session.flush()
    return row


def done_task(session: Session, finished_at: datetime, kind: str = "generic") -> Task:
    task = Task(title="t", kind=kind)
    session.add(task)
    session.flush()
    evidence.note(session, "proof", task_id=task.id)
    evidence.complete_task(session, task.id, {kind: ["proof"]})
    session.add(
        SopRun(
            id=str(uuid.uuid4()),
            role=ROLE,
            sop="s",
            task_id=task.id,
            status="done",
            finished_at=finished_at,
        )
    )
    session.flush()
    return task


def cost(session: Session, amount: str, at: datetime = MID) -> None:
    session.add(CostEvent(role=ROLE, amount_usd=Decimal(amount), kind="model", at=at))
    session.flush()


def test_empty_data_reports_nulls_not_zeros(session: Session) -> None:
    rep = report(session, SINCE, UNTIL)
    assert rep.model_dump(mode="json") == {
        "since": "2030-03-01T00:00:00Z",
        "until": "2030-03-29T00:00:00Z",
        "decision_types": [],
        "serious_errors": 0,
        "tasks_completed": 0,
        "cost_usd": 0.0,
        "cost_per_task_usd": None,
        "time_saved_minutes": None,
    }


def test_each_bucket(session: Session) -> None:
    op(session, "approved")
    op(session, "executed")
    op(session, "executed", "cosmetic", "cosmetic")
    op(session, "approved", "factual")
    op(session, "executed", "cosmetic", "policy")
    op(session, "executed", None)  # an untagged edit counts as an error
    op(session, "rejected")
    op(session, "rejected", "factual")  # edited, then rejected: still only a rejection
    op(session, "pending")
    op(session, "pending", "cosmetic")
    (row,) = report(session, SINCE, UNTIL).decision_types
    assert row.model_dump() == {
        "role": ROLE,
        "decision_type": KIND,
        "level": None,
        "proposals": 10,
        "approved_unchanged": 2,
        "approved_cosmetic": 1,
        "approved_with_errors": 3,
        "rejected": 2,
        "pending": 2,
        "approval_without_edit_rate": 2 / 6,  # rejections are not in the denominator
        "error_free_rate": 3 / 6,
    }


def test_rates_are_null_when_nothing_was_approved(session: Session) -> None:
    op(session, "rejected")
    op(session, "pending")
    (row,) = report(session, SINCE, UNTIL).decision_types
    assert row.proposals == 2
    assert row.approval_without_edit_rate is None and row.error_free_rate is None
    assert "not measured" in render(report(session, SINCE, UNTIL))


def test_groups_by_role_and_decision_type(session: Session) -> None:
    op(session, "approved")
    op(session, "approved", "factual", role="role_b")
    op(session, "approved", decision_type="other")
    rows = report(session, SINCE, UNTIL).decision_types
    assert [(r.role, r.decision_type) for r in rows] == [
        (ROLE, "other"),
        (ROLE, KIND),
        ("role_b", KIND),
    ]
    assert [r.error_free_rate for r in rows] == [1.0, 1.0, 0.0]


def test_no_decision_type_groups_under_empty_string_only_if_present(session: Session) -> None:
    op(session, "approved")
    assert [r.decision_type for r in report(session, SINCE, UNTIL).decision_types] == [KIND]
    op(session, "pending", decision_type=None)
    assert [r.decision_type for r in report(session, SINCE, UNTIL).decision_types] == ["", KIND]


def test_level_is_the_stored_level_or_null(session: Session) -> None:
    op(session, "approved")
    op(session, "approved", decision_type="other")
    autonomy.set_level(session, ROLE, KIND, 3, by="owner")
    autonomy.record_approval(session, ROLE, "other", unchanged=True)  # counters only, no level
    levels = {r.decision_type: r.level for r in report(session, SINCE, UNTIL).decision_types}
    assert levels == {KIND: 3, "other": None}


def test_window_is_since_inclusive_until_exclusive(session: Session) -> None:
    tick = timedelta(microseconds=1)
    for at in (SINCE - tick, SINCE, UNTIL - tick, UNTIL):
        op(session, "approved", at=at)
        cost(session, "1.5", at=at)
        done_task(session, at)
        session.add(
            AuditEntry(at=at, actor=ROLE, kind="autonomy", subject="x", detail={"how": "demote"})
        )
    session.flush()
    rep = report(session, SINCE, UNTIL)
    assert rep.decision_types[0].proposals == 2
    assert rep.cost_usd == 3.0
    assert rep.tasks_completed == 2
    assert rep.serious_errors == 2


def test_serious_errors_count_bad_outcomes_only(session: Session) -> None:
    now = datetime.now(UTC)
    since, until = now - timedelta(days=1), now + timedelta(days=1)
    autonomy.set_level(session, ROLE, KIND, 3, by="owner")
    autonomy.promote(session, ROLE, KIND, by="owner")
    autonomy.record_outcome(session, ROLE, KIND, good=True)
    assert report(session, since, until).serious_errors == 0
    autonomy.record_outcome(session, ROLE, KIND, good=False)
    autonomy.record_outcome(session, ROLE, "other", good=False)
    assert report(session, since, until).serious_errors == 2
    assert report(session, SINCE, UNTIL).serious_errors == 0


def test_tasks_completed_needs_a_done_task_and_a_finished_run(session: Session) -> None:
    task = done_task(session, MID)
    session.add(  # a second finished run for the same task is still one task
        SopRun(id="again", role=ROLE, sop="s", task_id=task.id, status="done", finished_at=MID)
    )
    open_task = Task(title="open")
    session.add(open_task)
    session.flush()
    session.add_all(
        [
            SopRun(id="rej", role=ROLE, sop="s", task_id=open_task.id, status="rejected",
                   finished_at=MID),
            SopRun(id="no-task", role=ROLE, sop="s", status="done", finished_at=MID),
        ]
    )  # fmt: skip
    session.flush()
    assert report(session, SINCE, UNTIL).tasks_completed == 1


def test_cost_per_task(session: Session) -> None:
    cost(session, "0.25")
    cost(session, "1.25")
    rep = report(session, SINCE, UNTIL)
    assert (rep.cost_usd, rep.cost_per_task_usd) == (1.5, None)
    for _ in range(4):
        done_task(session, MID)
    rep = report(session, SINCE, UNTIL)
    assert (rep.cost_usd, rep.cost_per_task_usd) == (1.5, 0.375)


def test_time_saved_with_and_without_a_baseline(session: Session) -> None:
    done_task(session, MID)
    done_task(session, MID, kind="long")
    op(session, "executed")
    op(session, "approved", "factual")
    op(session, "rejected")
    op(session, "pending")  # not reviewed yet
    assert report(session, SINCE, UNTIL).time_saved_minutes is None
    assert report(session, SINCE, UNTIL, {}).time_saved_minutes is None
    # 10 (default) + 30 (long) - 3 reviews x 2 minutes
    assert report(session, SINCE, UNTIL, BASELINE).time_saved_minutes == 34.0


def test_time_saved_can_be_negative(session: Session) -> None:
    op(session, "rejected")
    assert report(session, SINCE, UNTIL, BASELINE).time_saved_minutes == -2.0


def test_time_saved_is_null_when_a_task_kind_was_never_timed(session: Session) -> None:
    done_task(session, MID)
    config = {"baseline_minutes": {"long": 30}, "review_minutes_per_approval": 2}
    assert report(session, SINCE, UNTIL, config).time_saved_minutes is None


@pytest.mark.parametrize(
    "config",
    [
        {"baseline_minutes": {"default": 10}},  # review time never measured
        {"baseline_minutes": {"default": "ten"}, "review_minutes_per_approval": 2},
        {"baseline_minutes": {"default": -1}, "review_minutes_per_approval": 2},
        {"baseline_minutes": {"default": True}, "review_minutes_per_approval": 2},
        {"baseline_minutes": 10, "review_minutes_per_approval": 2},
        {"baseline_minutes": {"default": 10}, "review_minutes_per_approval": float("nan")},
        "ten minutes",
    ],
)
def test_an_unusable_baseline_is_a_doctor_problem_and_not_measured(
    session: Session, config: object
) -> None:
    assert config_problems(config)
    assert report(session, SINCE, UNTIL, config).time_saved_minutes is None  # type: ignore[arg-type]


@pytest.mark.parametrize("config", [None, {}, {"review_minutes_per_approval": 2}, BASELINE])
def test_usable_or_absent_baseline_has_no_problems(config: object) -> None:
    assert config_problems(config) == []


def test_report_writes_nothing(session: Session) -> None:
    op(session, "approved", "cosmetic")
    done_task(session, MID)

    def rows() -> int:
        return sum(
            session.scalar(select(func.count()).select_from(t)) or 0
            for t in (StagedOperation, Correction, AuditEntry, Task, SopRun, CostEvent)
        )

    before = rows()
    report(session, SINCE, UNTIL, BASELINE)
    assert not session.new and not session.dirty and not session.deleted
    assert rows() == before


def test_window_is_the_last_n_days() -> None:
    now = datetime(2030, 3, 29, tzinfo=UTC)
    assert window(28, now) == (SINCE, UNTIL)
