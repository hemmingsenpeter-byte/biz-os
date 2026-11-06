"""Policy decision types. Shared contract between tools, workflows and the policy engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SideEffect(StrEnum):
    READ = "read"
    DRAFT = "draft"
    EXTERNAL = "external"
    MONEY = "money"


GATED = (SideEffect.EXTERNAL, SideEffect.MONEY)


@dataclass(frozen=True)
class Action:
    """Something a role wants to do. `inner` lists tool names a composite action calls."""

    actor: str  # role name or user id
    tool: str
    side_effect: SideEffect
    inputs: dict[str, Any] = field(default_factory=dict)
    decision_type: str | None = None  # autonomy ladder key, e.g. "pricing"
    inner: tuple[str, ...] = ()
    amount: float | None = None


@dataclass(frozen=True)
class Allow:
    reason: str = "allowed"


@dataclass(frozen=True)
class NeedsApproval:
    approver: str
    reason: str = "approval required"


@dataclass(frozen=True)
class Deny:
    reason: str


Decision = Allow | NeedsApproval | Deny

# Strictness order used by the strictest-gate rule.
STRICTNESS = {Allow: 0, NeedsApproval: 1, Deny: 2}


def strictest(decisions: list[Decision]) -> Decision:
    if not decisions:
        return Allow()
    return max(decisions, key=lambda d: STRICTNESS[type(d)])
