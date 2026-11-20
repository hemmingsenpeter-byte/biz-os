"""Process execution mode and evidence for suppressed provider writes."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.policy.types import SideEffect

SHADOW_REASON = "External and money actions are disabled in shadow mode"
SHADOW_EVIDENCE = "shadow.suppressed"


class ExecutionMode(StrEnum):
    SHADOW = "shadow"
    SUPERVISED = "supervised"


def configured_mode(default: str = ExecutionMode.SUPERVISED) -> ExecutionMode:
    """Read the operator override once at startup; reject an unknown mode."""
    try:
        return ExecutionMode(os.environ.get("BIZ_EXECUTION_MODE") or default)
    except ValueError:
        raise ValueError("BIZ_EXECUTION_MODE must be shadow or supervised") from None


@dataclass(frozen=True)
class Suppression:
    audit_id: uuid.UUID


def record_suppression(
    session: Session,
    actor: str,
    tool: str,
    effects: set[SideEffect],
    args: dict[str, Any],
    decision_type: str | None = None,
) -> Suppression:
    """Keep addresses and resource references for review, without message bodies or secrets."""
    recipients = []
    for key in ("to", "attendees"):
        value = args.get(key)
        if isinstance(value, str):
            recipients.append(value)
        if isinstance(value, list):
            recipients.extend(item for item in value if isinstance(item, str))
    if isinstance(signers := args.get("signers"), list):
        recipients.extend(
            signer["email"]
            for signer in signers
            if isinstance(signer, dict) and isinstance(signer.get("email"), str)
        )
    refs = {
        key: args[key]
        for key in ("message_id", "invoice_id", "bill_id", "document_id", "change_id")
        if isinstance(args.get(key), str)
    }
    idempotency_key = args.get("idempotency_key")
    entry = record(
        session,
        actor,
        SHADOW_EVIDENCE,
        tool,
        mode=ExecutionMode.SHADOW,
        effects=sorted(effects),
        recipients=list(dict.fromkeys(recipients)),
        references=refs,
        decision_type=decision_type,
        idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
    )
    return Suppression(entry.id)
