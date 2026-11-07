"""escalate_when: the generic condition evaluator (parse strictly, fail closed)."""

from __future__ import annotations

import pytest
from biz_kernel.workflows.escalation import ConditionError, check, parse

RAW = [{"confidence_below": 0.7}, "some_signal"]


def test_parse_accepts_declared_forms() -> None:
    conds = parse(RAW, known_signals={"some_signal"})
    assert [c.name for c in conds] == ["confidence_below", "some_signal"]
    assert parse([], known_signals=set()) == ()


@pytest.mark.parametrize(
    "bad",
    [
        ["no_such_signal"],
        [{"confidence_below": "high"}],
        [{"confidence_below": True}],
        [{"confidence_below": 1.5}],
        [{"confidence_below": -0.1}],
        [{"made_up": 1}],
        [{"confidence_below": 0.5, "other": 1}],
        [3],
        "confidence_below",
        [None],
    ],
)
def test_parse_rejects_unknown_or_malformed(bad: object) -> None:
    with pytest.raises(ConditionError):
        parse(bad, known_signals={"some_signal"})


def test_confidence_below_threshold_escalates() -> None:
    conds = parse([{"confidence_below": 0.7}], known_signals=set())
    assert check(conds, kind="draft", output={"confidence": 0.69})
    assert check(conds, kind="draft", output={"confidence": 0.7}) is None
    assert check(conds, kind="draft", output={"confidence": 1}) is None


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"confidence": None},
        {"confidence": "0.9"},
        {"confidence": True},
        {"confidence": float("nan")},
        "text",
        None,
    ],
)
def test_missing_or_bad_confidence_fails_closed(output: object) -> None:
    conds = parse([{"confidence_below": 0.7}], known_signals=set())
    assert check(conds, kind="draft", output=output)


def test_confidence_is_only_read_from_draft_steps() -> None:
    conds = parse([{"confidence_below": 0.7}], known_signals=set())
    assert check(conds, kind="tool", output={"rows": 3}) is None


def test_signal_matches_any_step_kind() -> None:
    conds = parse(["some_signal"], known_signals={"some_signal"})
    assert check(conds, kind="tool", output={"signals": ["some_signal"]})
    assert check(conds, kind="draft", output={"signals": ["some_signal"]})
    assert check(conds, kind="tool", output={"signals": []}) is None
    assert check(conds, kind="tool", output={"x": 1}) is None
    assert check(conds, kind="tool", output=None) is None


def test_sop_keeps_control_of_what_it_handles() -> None:
    from biz_kernel.workflows.runner import _conditions_by_step

    conds = parse([{"confidence_below": 0.7}, "sig"], known_signals={"sig"})
    reads = {
        "steps": [
            {"id": "d", "kind": "draft"},
            {"id": "p", "kind": "tool", "args": {"c": "{{steps.d.output.confidence}}"}},
        ]
    }
    own = {"steps": [{"id": "d", "kind": "draft"}, {"id": "s", "kind": "stop"}]}
    assert [c.name for c in _conditions_by_step(reads, conds)["d"]] == ["sig"]
    assert [c.name for c in _conditions_by_step(own, conds)["d"]] == ["confidence_below"]
