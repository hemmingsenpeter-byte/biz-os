"""Comms tools. draft/queue/classify/opt_out are drafts; send is external (policy-gated)."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
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
from biz_cap_comms.gate import GateConfig, normalize_address
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


MIN_CONFIDENCE = 0.7
INTENTS = ("choose_slot", "reschedule", "question", "decline", "other")


def _fence(text: str) -> str:
    """Customer text, quoted and clearly marked so it cannot pass as system text."""
    flat = " ".join(str(text).split())[:200].replace("«", "<").replace("»", ">")
    return f"Customer wrote (unverified): «{flat}»"


def _aware(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _label(start: str) -> str:
    return _aware(start).astimezone(UTC).strftime("%a %d %b %Y %H:%M UTC")


def _slots(row: Outbox) -> list[dict[str, str]]:
    raw = (row.meta or {}).get("offered_slots") or []
    return [s for s in raw if isinstance(s, dict) and s.get("start") and s.get("end")]


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
    calendar = ports.get("calendar")  # optional: only used to re-check an offered slot
    telephony: TelephonyPort = ports["telephony"]

    def _provider_call(row: Outbox, send: bool) -> SendResult | None:
        """Send (or reconcile) through the port that matches the channel."""
        key = row.idempotency_key
        if row.channel == "mail":
            if send:
                subject = row.subject
                if row.reply_token and f"[ref:{row.reply_token}]" not in subject:
                    subject = f"{subject} [ref:{row.reply_token}]".strip()  # threads the reply
                msg = OutboundMessage(to=[row.to], subject=subject, body=row.body)
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
        meta: dict[str, Any] | None = None,
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
            reply_token=secrets.token_hex(5) if channel == "mail" else None,
            reply_expires_at=(
                datetime.now(UTC) + timedelta(days=config.reply_token_days())
                if channel == "mail"
                else None
            ),
            meta=meta,
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

    def read_thread(session: Session, message_id: str) -> dict[str, Any]:
        """What was sent to the customer, and the slots offered (numbered from 0)."""
        row = _row(session, message_id)
        slots = [{"index": i, **s, "label": _label(s["start"])} for i, s in enumerate(_slots(row))]
        return {
            **_view(row),
            "party_id": str(row.party_id or ""),
            "enquiry_id": str((row.meta or {}).get("enquiry_id") or ""),
            "offered_slots": slots,
        }

    def match_reply(session: Session, reply_token: str, sender: str) -> dict[str, Any]:
        """The sent mail a reply token belongs to, only if `sender` is the address written to.

        A different sender is not a reply to that thread, so nobody else can hijack it.
        An expired token matches nothing, so the mail is handled as a new enquiry."""
        token = str(reply_token or "").strip().lower()
        no = {"matched": False, "message_id": "", "party_id": "", "enquiry_id": ""}
        if not token:
            return no
        row = session.scalar(
            select(Outbox).where(
                Outbox.reply_token == token,
                Outbox.channel == "mail",
                Outbox.status == "sent",
                (Outbox.reply_expires_at.is_(None)) | (Outbox.reply_expires_at > utcnow()),
            )
        )
        if row is None or normalize_address(sender) != normalize_address(row.expected_to or row.to):
            return no
        return {
            "matched": True,
            "message_id": str(row.id),
            "party_id": str(row.party_id or ""),
            "enquiry_id": str((row.meta or {}).get("enquiry_id") or ""),
        }

    def resolve_reply(
        session: Session,
        message_id: str,
        intent: str,
        confidence: float,
        slot_index: int | None = None,
        reply_text: str = "",
        sender_verified: bool = False,
    ) -> dict[str, Any]:
        """Turn the model's reading of a reply into `book` or `escalate`. The slot must be one
        that was offered, and still free; anything doubtful escalates to the owner."""
        row = _row(session, message_id)
        slots = _slots(row)
        out: dict[str, Any] = {
            "action": "escalate",
            "reason": "",
            "intent": intent,
            "slot_index": -1,
            "start": "",
            "end": "",
            "slot_label": "",
            "customer_text": _fence(reply_text),
            "sender_verified": sender_verified is True,
        }
        out["sender_note"] = "" if out["sender_verified"] else "unverified sender"
        expires = row.reply_expires_at
        proposed = (row.meta or {}).get("booking_proposed")
        digest = hashlib.sha256(reply_text.encode()).hexdigest()[:16]
        if row.status != "sent" or intent not in INTENTS:
            out["reason"] = "reply does not match a sent message or the intent is unknown"
        elif expires is not None and expires < datetime.now(UTC):
            out["reason"] = "reply token expired"
        elif (
            intent == "choose_slot"
            and isinstance(proposed, dict)
            and proposed.get("reply") != digest
        ):
            out["reason"] = "a booking was already proposed for this thread"
        elif intent != "choose_slot":
            out["reason"] = f"customer reply needs a person: {intent}"
        elif not isinstance(confidence, int | float) or confidence < MIN_CONFIDENCE:
            out["reason"] = f"low confidence reading the reply ({confidence})"
        elif (
            isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or not 0 <= slot_index < len(slots)
        ):
            out["reason"] = f"chosen slot {slot_index} is not one of the {len(slots)} offered"
        else:
            slot = slots[slot_index]
            start, end = _aware(slot["start"]), _aware(slot["end"])
            out.update(
                slot_index=slot_index,
                start=start.isoformat(),
                end=end.isoformat(),
                slot_label=_label(slot["start"]),
            )
            free = True
            if calendar is not None:
                minutes = int((end - start).total_seconds() // 60)
                got = asyncio.run(calendar.free_slots(start, end, minutes))
                free = start > datetime.now(UTC) and any(sl.start == start for sl in got)
            if free:
                out["action"] = "book"
                # one booking per thread; the same reply re-read (a replay) stays allowed
                row.meta = {
                    **(row.meta or {}),
                    "booking_proposed": {
                        "slot_index": slot_index,
                        "reply": digest,
                        "at": datetime.now(UTC).isoformat(),
                    },
                }
                session.flush()
            else:
                out["reason"] = f"{out['slot_label']} is no longer free"
        return out

    def opt_out(
        session: Session, address: str, source: str = "manual", party_id: str | None = None
    ) -> dict[str, Any]:
        row = service.add_opt_out(
            session, address, source, uuid.UUID(party_id) if party_id else None
        )
        return {"address": row.address, "opted_out": True}

    def is_suppressed(
        session: Session, address: str, party_id: str | None = None
    ) -> dict[str, Any]:
        """The one suppression list: other capabilities ask here before they send."""
        pid = uuid.UUID(party_id) if party_id else None
        return {
            "address": normalize_address(address),
            "suppressed": service.is_suppressed(session, address, pid),
        }

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
                    "meta": {"type": "object"},
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
            f"{CAP}.read_thread",
            CAP,
            SideEffect.READ,
            _obj({"message_id": _STR}, ["message_id"]),
            read_thread,
            "Read a sent message and the meeting slots it offered",
        ),
        Tool(
            f"{CAP}.match_reply",
            CAP,
            SideEffect.READ,
            _obj({"reply_token": _STR, "sender": _STR}, ["reply_token", "sender"]),
            match_reply,
            "Find the sent mail a reply token belongs to, if the sender is who it was sent to",
        ),
        Tool(
            f"{CAP}.resolve_reply",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "message_id": _STR,
                    "intent": _STR,
                    "confidence": {"type": "number"},
                    "slot_index": {"type": ["integer", "null"]},
                    "reply_text": _STR,
                    "sender_verified": {"type": "boolean"},
                },
                ["message_id", "intent", "confidence"],
            ),
            resolve_reply,
            "Validate the model's reading of a reply: book an offered, free slot, or escalate",
        ),
        Tool(
            f"{CAP}.opt_out",
            CAP,
            SideEffect.DRAFT,
            _obj({"address": _STR, "source": _STR, "party_id": _STR}, ["address"]),
            opt_out,
            "Record a permanent suppression for an address or number",
        ),
        Tool(
            f"{CAP}.is_suppressed",
            CAP,
            SideEffect.READ,
            _obj({"address": _STR, "party_id": _STR}, ["address"]),
            is_suppressed,
            "Whether an address or number (or its party) has opted out",
        ),
    ]
    for t in tools:
        registry.register(t)
