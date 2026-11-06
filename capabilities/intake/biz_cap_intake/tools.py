"""Intake tools. classify/create are drafts; text_back and reply are external (policy-gated)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from biz_kernel.contracts.ports import MailPort, OutboundMessage, TelephonyPort
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy.orm import Session

from biz_cap_intake import config, service
from biz_cap_intake.models import Enquiry, SentText

CAP = "intake"
LOW_CONFIDENCE = 0.6


class UncertainSend(RuntimeError):
    """The provider may have sent it. Never retried until reconciled."""


class Suppressed(PermissionError):
    """The recipient opted out."""


def _enquiry(session: Session, enquiry_id: str) -> Enquiry:
    row = session.get(Enquiry, uuid.UUID(enquiry_id))
    if row is None:
        raise LookupError(f"enquiry {enquiry_id} not found")
    return row


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": props,
    }


_STR: dict[str, Any] = {"type": "string"}


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    telephony: TelephonyPort = ports["telephony"]
    mail: MailPort = ports["mail"]

    def classify(session: Session, enquiry_id: str) -> dict[str, Any]:
        row = _enquiry(session, enquiry_id)
        out = service.classify_rules(row.channel, row.sender, row.subject, row.body)
        out["enquiry"] = {
            "channel": row.channel,
            "from": row.sender,
            "subject": row.subject,
            "body": row.body,
        }
        return out

    def create_enquiry(
        session: Session,
        enquiry_id: str | None = None,
        channel: str = "phone",
        sender: str = "",
        subject: str = "",
        body: str = "",
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        if enquiry_id:
            row = _enquiry(session, enquiry_id)
        else:
            row = Enquiry(
                channel=channel, sender=sender, subject=subject, body=body, provider_id=provider_id
            )
            session.add(row)
            session.flush()
        party_id = service.ensure_party(session, row)
        return {
            "enquiry_id": str(row.id),
            "party_id": str(party_id),
            "can_text": not service.is_opted_out(session, row.sender),
        }

    def read_enquiry(session: Session, enquiry_id: str) -> dict[str, Any]:
        row = _enquiry(session, enquiry_id)
        return {
            "enquiry_id": str(row.id),
            "channel": row.channel,
            "from": row.sender,
            "subject": row.subject,
            "body": row.body,
            "party_id": str(row.party_id or ""),
            "category": row.classified_as,
        }

    def set_classification(
        session: Session,
        enquiry_id: str,
        rule: dict[str, Any],
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Combine the rule result and (when rules did not decide) the model's answer."""
        row = _enquiry(session, enquiry_id)
        model = model or {}
        reason = rule.get("reason")
        confidence = None
        if rule.get("needs_model"):
            category = str(model.get("category", "other"))
            confidence = model.get("confidence")
            summary = str(model.get("summary") or row.subject or row.body)[:500]
            if category not in service.CATEGORIES:
                category, reason = "other", "invalid_category"
            elif isinstance(confidence, int | float) and confidence < LOW_CONFIDENCE:
                category, reason = "other", "low_confidence"
        else:
            category = str(rule["category"])
            summary = (row.subject or row.body)[:500]
        if reason == "opt_out":
            service.add_opt_out(session, row.sender, "triage")
        row.classified_as, row.reason, row.summary = category, reason, summary
        row.confidence = float(confidence) if isinstance(confidence, int | float) else None
        row.status = "classified"
        party_id = None
        if category in ("enquiry", "service_request"):
            party_id = str(service.ensure_party(session, row))
        session.flush()
        return {
            "enquiry_id": str(row.id),
            "category": category,
            "reason": reason or category,
            "summary": summary,
            "channel": row.channel,
            "contact": row.sender,
            "subject": row.subject,
            "body": row.body,
            "party_id": party_id or "",
            "opportunity_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"biz:opportunity:{row.id}")),
        }

    def _send_once(session: Session, key: str, to: str, body: str, send: Any) -> dict[str, Any]:
        """Send through a port exactly once per key; a replay returns the first result."""
        prior = session.get(SentText, key)
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"biz:{key}"))
        if prior is not None:
            return {
                "message_id": message_id,
                "provider_id": prior.provider_id,
                "to": to,
                "body": body,
            }
        result = asyncio.run(send())
        if result.status == "uncertain":
            raise UncertainSend(key)
        if result.status != "sent":
            raise RuntimeError(result.error or "send failed")
        session.add(SentText(key=key, to=to, body=body, provider_id=result.provider_id))
        session.flush()
        return {"message_id": message_id, "provider_id": result.provider_id, "to": to, "body": body}

    def text_back(session: Session, to: str, idempotency_key: str) -> dict[str, Any]:
        if service.is_opted_out(session, to):
            raise Suppressed(f"{to} opted out")
        body = config.text_back_template()  # fixed text; model output never reaches this path
        out = _send_once(
            session,
            idempotency_key,
            to,
            body,
            lambda: telephony.send_sms(to, body, idempotency_key),
        )
        return {**out, "channel": "sms"}

    def reply(
        session: Session, to: str, subject: str, body: str, idempotency_key: str
    ) -> dict[str, Any]:
        if service.is_opted_out(session, to):
            raise Suppressed(f"{to} opted out")
        if "@" in to:
            msg = OutboundMessage(to=[to], subject=subject, body=body)
            out = _send_once(
                session, idempotency_key, to, body, lambda: mail.send(msg, idempotency_key)
            )
            return {**out, "channel": "mail", "subject": subject}
        out = _send_once(
            session,
            idempotency_key,
            to,
            body,
            lambda: telephony.send_sms(to, body, idempotency_key),
        )
        return {**out, "channel": "sms", "subject": subject}

    idem: dict[str, Any] = {"idempotency_key": _STR}
    tools = [
        Tool(
            f"{CAP}.classify",
            CAP,
            SideEffect.DRAFT,
            _obj({"enquiry_id": _STR}, ["enquiry_id"]),
            classify,
            "Deterministic first-pass triage",
        ),
        Tool(
            f"{CAP}.create_enquiry",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "enquiry_id": _STR,
                    "channel": _STR,
                    "sender": _STR,
                    "subject": _STR,
                    "body": _STR,
                    "provider_id": _STR,
                }
            ),
            create_enquiry,
            "Write or find the enquiry row and its party",
        ),
        Tool(
            f"{CAP}.read_enquiry",
            CAP,
            SideEffect.READ,
            _obj({"enquiry_id": _STR}, ["enquiry_id"]),
            read_enquiry,
            "Read an enquiry row",
        ),
        Tool(
            f"{CAP}.set_classification",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"enquiry_id": _STR, "rule": {"type": "object"}, "model": {"type": "object"}},
                ["enquiry_id", "rule"],
            ),
            set_classification,
            "Store the final category and prepare routing data",
        ),
        Tool(
            f"{CAP}.text_back",
            CAP,
            SideEffect.EXTERNAL,
            _obj({"to": _STR, **idem}, ["to", "idempotency_key"]),
            text_back,
            "Send the fixed templated text-back SMS",
        ),
        Tool(
            f"{CAP}.reply",
            CAP,
            SideEffect.EXTERNAL,
            _obj(
                {"to": _STR, "subject": _STR, "body": _STR, **idem},
                ["to", "subject", "body", "idempotency_key"],
            ),
            reply,
            "Reply to an enquirer",
        ),
    ]
    for t in tools:
        registry.register(t)
