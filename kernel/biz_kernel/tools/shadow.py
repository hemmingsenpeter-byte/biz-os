"""Process execution mode and evidence for suppressed provider writes."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.policy.types import Allow, Decision, Deny, NeedsApproval, SideEffect

SHADOW_REASON = "External and money actions are disabled in shadow mode"
SHADOW_EVIDENCE = "shadow.suppressed"


class ExecutionMode(StrEnum):
    SHADOW = "shadow"
    SUPERVISED = "supervised"


# Strictness order: an operator override may move down this list, never up it.
STRICTNESS = {ExecutionMode.SUPERVISED: 0, ExecutionMode.SHADOW: 1}
AUTHORIZATION_KEY = "supervised_authorization"
AUTHORIZATION_FIELDS = ("authorized_by", "authorized_on", "pilot_exit_evidence")
CLOUD_MODEL_SLOT = "model_cloud"
CLOUD_MODEL_PORTS = frozenset({"model_claude"})  # adapters that send prompts off the appliance


class ExecutionModeError(ValueError):
    """The composition may not run in the requested mode."""


def configured_mode(default: str = ExecutionMode.SUPERVISED) -> ExecutionMode:
    """Read the operator override once at startup; reject an unknown mode.

    Only for callers with no composition. A composition resolves its mode with
    `resolve_mode`, which never lets the override loosen it.
    """
    try:
        return ExecutionMode(os.environ.get("BIZ_EXECUTION_MODE") or default)
    except ValueError:
        raise ValueError("BIZ_EXECUTION_MODE must be shadow or supervised") from None


def authorization_problems(authorization: Any, today: date | None = None) -> list[str]:
    """Why a composition's record of the owner's go-ahead for supervised mode is unusable."""
    if not isinstance(authorization, Mapping):
        return [
            f"supervised mode needs {AUTHORIZATION_KEY} with "
            + ", ".join(AUTHORIZATION_FIELDS)
            + " recorded in the composition"
        ]
    problems = []
    for key in ("authorized_by", "pilot_exit_evidence"):
        value = authorization.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{AUTHORIZATION_KEY}.{key} must be set")
    raw = authorization.get("authorized_on")
    when: date | None = None
    if isinstance(raw, datetime):
        when = raw.date()
    elif isinstance(raw, date):
        when = raw
    elif isinstance(raw, str):
        try:
            when = date.fromisoformat(raw)
        except ValueError:
            when = None
    if when is None:
        problems.append(f"{AUTHORIZATION_KEY}.authorized_on must be a date (YYYY-MM-DD)")
    elif when > (today or date.today()):
        problems.append(f"{AUTHORIZATION_KEY}.authorized_on cannot be in the future")
    return problems


def _adapter_instance(port: Any, module: str, name: str) -> bool:
    return any(cls.__module__ == module and cls.__name__ == name for cls in type(port).__mro__)


def cloud_model_slots(ports: Mapping[str, Any]) -> list[str]:
    """Port slots that would send model prompts to a cloud provider."""
    return sorted(
        slot
        for slot, port in ports.items()
        if slot == CLOUD_MODEL_SLOT
        or (isinstance(port, str) and port in CLOUD_MODEL_PORTS)
        or _adapter_instance(port, "biz_port_model_claude", "ClaudeModel")
    )


def allows_cloud_in_shadow(policy: Mapping[str, Any]) -> bool:
    section = policy.get("shadow")
    return isinstance(section, Mapping) and section.get("allow_cloud_model") is True


def _ollama_problems(
    ports: Mapping[str, Any], environ: Mapping[str, str], *, compose: bool
) -> list[str]:
    """Check configured settings and actual Ollama override objects without any requests."""
    # Inspect the concrete adapter, including subclasses, rather than unrelated objects
    # with similarly named attributes. No provider is constructed for this check.
    candidates = [
        port
        for port in ports.values()
        if (isinstance(port, str) and port == "model_ollama")
        or _adapter_instance(port, "biz_port_model_ollama", "OllamaModel")
    ]
    if not candidates:
        return []
    from biz_port_model_ollama.config import OllamaSettings

    problems = []
    for port in candidates:
        settings = (
            OllamaSettings.from_env(environ)
            if isinstance(port, str)
            else OllamaSettings(port.base_url, port.model, port.embed_model)
        )
        problems.extend(settings.locality_problems(compose=compose))
    return list(dict.fromkeys(problems))


def mode_problems(
    configured: Any,
    authorization: Any,
    *,
    override: str | None,
    ports: Mapping[str, Any],
    policy: Mapping[str, Any],
    today: date | None = None,
    environ: Mapping[str, str] | None = None,
    compose: bool = False,
) -> tuple[ExecutionMode | None, list[str]]:
    """(effective mode, problems). Any problem means the composition must not start.

    The composition's configured mode is the loosest it may run. The operator override
    (BIZ_EXECUTION_MODE) can only make it stricter. Supervised needs the owner's recorded
    authorization; shadow refuses a cloud model unless the policy explicitly allows it.
    """
    try:
        base = ExecutionMode(configured)
    except ValueError:
        return None, ["execution_mode must be shadow or supervised"]
    effective = base
    if override:
        try:
            requested = ExecutionMode(override)
        except ValueError:
            return None, ["BIZ_EXECUTION_MODE must be shadow or supervised"]
        if STRICTNESS[requested] < STRICTNESS[base]:
            return None, [
                f"BIZ_EXECUTION_MODE={requested} cannot loosen the composition's {base} mode; "
                "leaving shadow mode needs the owner's authorization in the composition"
            ]
        effective = requested
    problems: list[str] = []
    if effective == ExecutionMode.SUPERVISED:
        problems += authorization_problems(authorization, today)
    if effective == ExecutionMode.SHADOW and not allows_cloud_in_shadow(policy):
        problems += [
            f"port {slot} is a cloud model; shadow mode needs policy shadow.allow_cloud_model: true"
            for slot in cloud_model_slots(ports)
        ]
    if effective == ExecutionMode.SHADOW and (compose or not allows_cloud_in_shadow(policy)):
        problems += _ollama_problems(
            ports, os.environ if environ is None else environ, compose=compose
        )
    return effective, problems


def resolve_mode(
    configured: Any,
    authorization: Any,
    *,
    ports: Mapping[str, Any],
    policy: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> ExecutionMode:
    """The composition's effective mode, or ExecutionModeError naming every problem."""
    env = os.environ if environ is None else environ
    mode, problems = mode_problems(
        configured,
        authorization,
        override=env.get("BIZ_EXECUTION_MODE") or None,
        ports=ports,
        policy=policy,
        environ=env,
    )
    if problems or mode is None:
        raise ExecutionModeError("; ".join(problems))
    return mode


def verdict(decision: Decision | None) -> dict[str, str] | None:
    """What policy would have decided, as stored on a suppression record."""
    if decision is None:
        return None
    kind = {Allow: "allow", NeedsApproval: "ask", Deny: "deny"}[type(decision)]
    return {"decision": kind, "reason": decision.reason}


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
    policy: dict[str, str] | None = None,
    pre_approved: bool = False,
    staged_op_id: str | None = None,
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
        policy=policy,  # the would-be verdict, evaluated without side effects
        pre_approved=pre_approved,
        staged_op_id=staged_op_id,
    )
    return Suppression(entry.id)
