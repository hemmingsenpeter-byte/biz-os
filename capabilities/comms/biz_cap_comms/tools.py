"""Comms tools. draft/queue/classify/opt_out are drafts; send is external (policy-gated)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.contracts.ports import MailPort, OutboundMessage, SendResult, TelephonyPort
from biz_kernel.db import utcnow
from biz_kernel.health.service import raise_alert
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_comms import config, service
from biz_cap_comms.gate import GateConfig
from biz_cap_comms.models import Outbox

CAP = "comms"
_STR: dict[str, Any] = {"type": "string"}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": props,
    }


def _row(session: Session, message_id: str) -> Outbox:
    row = session.get(Outbox, uuid.UUID(message_id))
    if row is None:
        raise LookupError(f"message {message_id} not found")
    return row


def _view(row: Outbox) -> dict[str, Any]:
    return {
        "message_id": str(row.id),
        "channel": row.channel,
        "to": row.to,
        "subject": row.subject,
        "body": row.body,
        "status": row.status,
        "provider_id": row.provider_id,
        "attempts": row.attempts,
        "reasons": (row.gate_report or {}).get("reasons", []),
    }


def register(
    registry: ToolRegistry, ports: dict[str, Any], gate_config: GateConfig | None = None
) -> None:
    """`gate_config` (banned phrases, link domains, lengths) overrides composition policy."""
    if gate_config is not None:
        config.configure(gate_config)
    mail: MailPort = ports["mail"]
    telephony: TelephonyPort = ports["telephony"]

    def _provider_call(row: Outbox, send: bool) -> SendResult | None:
        """Send (or reconcile) through the port that matches the channel."""
        key = row.idempotency_key
        if row.channel == "mail":
            if send:
                msg = OutboundMessage(to=[row.to], subject=row.subject, body=row.body)
                return asyncio.run(mail.send(msg, key))
            fn = getattr(mail, "reconcile", None)
        else:
            if send:
                return asyncio.run(telephony.send_sms(row.to, row.body, key))
            fn = getattr(telephony, "reconcile", None)
        if fn is None:
            return None
        out = fn(key)
        return asyncio.run(out) if asyncio.iscoroutine(out) else out

    def draft(
        session: Session,
        channel: str,
        to: str,
        body: str,
        subject: str = "",
        idempotency_key: str | None = None,
        party_id: str | None = None,
        expected_to: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or f"comms:{uuid.uuid4()}"
        existing = session.scalar(select(Outbox).where(Outbox.idempotency_key == key))
        if existing is not None:
            return _view(existing)
        row = Outbox(
            channel=channel,
            to=to.strip(),
            subject=subject,
            body=body,
            idempotency_key=key,
            party_id=uuid.UUID(party_id) if party_id else None,
            expected_to=expected_to,
        )
        session.add(row)
        session.flush()
        return _view(row)

    def queue(session: Session, message_id: str) -> dict[str, Any]:
        row = _row(session, message_id)
        if row.status not in ("draft", "queued", "blocked", "failed"):
            return _view(row)  # sending/sent/uncertain are never re-queued
        report = service.gate_for(session, row, config.gate_config())
        row.gate_report = report.as_dict()
        row.status = "queued" if report.passed else "blocked"
        session.flush()
        return _view(row)

    def send(
        session: Session,
        message_id: str,
        idempotency_key: str,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        row = _row(session, message_id)
        # The approver approves what they see: the content in the call must be the row's.
        for name, shown, actual in (
            ("to", to, row.to),
            ("subject", subject, row.subject),
            ("body", body, row.body),
        ):
            if shown is not None and shown != (actual or ""):
                raise PermissionError(f"{name} differs from the approved message")
        if row.status == "sent":
            return _view(row)  # replay: first result
        if row.status in ("uncertain", "sending"):
            # a message found mid-flight is treated as uncertain; only reconcile resolves it
            row.status = "uncertain"
            session.flush()
            return _view(row)
        if row.status != "queued":
            raise PermissionError(f"message is {row.status}, only queued messages are sent")
        # Re-gate at send time: opt-outs recorded after queueing still block.
        report = service.gate_for(session, row, config.gate_config())
        if not report.passed:
            row.gate_report, row.status = report.as_dict(), "blocked"
            session.flush()
            return _view(row)
        row.status, row.attempts = "sending", row.attempts + 1
        session.flush()
        result = _provider_call(row, True)
        assert result is not None
        if result.status == "sent":
            row.status, row.provider_id, row.sent_at = "sent", result.provider_id, utcnow()
            record(session, "comms", "comms.sent", str(row.id), to=row.to, channel=row.channel)
        elif result.status == "uncertain":
            row.status = "uncertain"
            raise_alert(
                session,
                "comms.uncertain_send",
                str(row.id),
                f"send to {row.to} may or may not have gone out; reconcile before any retry",
                utcnow(),
            )
        else:
            row.status = "failed"
            row.gate_report = {**(row.gate_report or {}), "error": result.error}
        session.flush()
        return _view(row)

    def reconcile(session: Session, message_id: str) -> dict[str, Any]:
        row = _row(session, message_id)
        if row.status != "uncertain":
            return _view(row)
        result = _provider_call(row, False)
        if result is None or result.status == "uncertain":
            return {**_view(row), "reconciled": False}
        if result.status == "sent":
            row.status, row.provider_id, row.sent_at = "sent", result.provider_id, utcnow()
        else:
            row.status = "failed"  # the provider confirms nothing went out: may be queued again
        record(session, "comms", "comms.reconciled", str(row.id), status=row.status)
        session.flush()
        return {**_view(row), "reconciled": True}

    def classify_reply(
        session: Session,
        channel: str,
        sender: str,
        body: str,
        subject: str = "",
        party_id: str | None = None,
    ) -> dict[str, Any]:
        out = service.classify_reply_rules(channel, sender, subject, body)
        if out["reason"] == "opt_out":  # recorded before any model sees the message
            service.add_opt_out(
                session, sender, f"reply_{channel}", uuid.UUID(party_id) if party_id else None
            )
        return out

    def opt_out(
        session: Session, address: str, source: str = "manual", party_id: str | None = None
    ) -> dict[str, Any]:
        row = service.add_opt_out(
            session, address, source, uuid.UUID(party_id) if party_id else None
        )
        return {"address": row.address, "opted_out": True}

    idem: dict[str, Any] = {"idempotency_key": _STR}
    tools = [
        Tool(
            f"{CAP}.draft",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "channel": {"type": "string", "enum": ["mail", "sms"]},
                    "to": _STR,
                    "subject": _STR,
                    "body": _STR,
                    "idempotency_key": _STR,
                    "party_id": _STR,
                    "expected_to": _STR,
                },
                ["channel", "to", "body"],
            ),
            draft,
            "Store a drafted message in the outbox",
        ),
        Tool(
            f"{CAP}.queue",
            CAP,
            SideEffect.DRAFT,
            _obj({"message_id": _STR}, ["message_id"]),
            queue,
            "Run the send gate: queued or blocked",
        ),
        Tool(
            f"{CAP}.send",
            CAP,
            SideEffect.EXTERNAL,
            _obj(
                {"message_id": _STR, "to": _STR, "subject": _STR, "body": _STR, **idem},
                ["message_id", "to", "body", "idempotency_key"],
            ),
            send,
            "Send a queued message once; uncertain sends wait for reconcile",
        ),
        Tool(
            f"{CAP}.reconcile",
            CAP,
            SideEffect.READ,
            _obj({"message_id": _STR}, ["message_id"]),
            reconcile,
            "Resolve an uncertain send with the provider",
        ),
        Tool(
            f"{CAP}.classify_reply",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "channel": _STR,
                    "sender": _STR,
                    "subject": _STR,
                    "body": _STR,
                    "party_id": _STR,
                },
                ["channel", "sender", "body"],
            ),
            classify_reply,
            "Opt-out and auto-reply rules before any model",
        ),
        Tool(
            f"{CAP}.opt_out",
            CAP,
            SideEffect.DRAFT,
            _obj({"address": _STR, "source": _STR, "party_id": _STR}, ["address"]),
            opt_out,
            "Record a permanent suppression for an address or number",
        ),
    ]
    for t in tools:
        registry.register(t)
