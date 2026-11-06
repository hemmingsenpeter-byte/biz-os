"""Tools registered by demo_desk. send_reply is `external`, so it always passes policy.check."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from biz_kernel.contracts.ports import MailPort, OutboundMessage
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy.orm import Session

from biz_cap_demo_desk.models import DemoDraft, DemoRequest

CAP = "demo_desk"


class UncertainSend(RuntimeError):
    """The provider may have sent it. Never retried until reconciled."""


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    mail: MailPort = ports["mail"]

    def read_request(session: Session, request_id: str) -> dict[str, Any]:
        req = session.get(DemoRequest, uuid.UUID(request_id))
        if req is None:
            raise LookupError(f"request {request_id} not found")
        return {"id": str(req.id), "from": req.sender, "subject": req.subject, "body": req.body}

    def draft_reply(session: Session, request_id: str, subject: str, body: str) -> dict[str, Any]:
        draft = DemoDraft(request_id=uuid.UUID(request_id), subject=subject, body=body)
        session.add(draft)
        session.flush()
        return {"draft_id": str(draft.id)}

    def send_reply(
        session: Session, to: str, subject: str, body: str, idempotency_key: str
    ) -> dict[str, Any]:
        msg = OutboundMessage(to=[to], subject=subject, body=body)
        result = asyncio.run(mail.send(msg, idempotency_key))
        if result.status == "uncertain":
            raise UncertainSend(idempotency_key)
        if result.status != "sent":
            raise RuntimeError(result.error or "send failed")
        message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"biz:{idempotency_key}")
        return {"message_id": str(message_id), "provider_id": result.provider_id}

    s = {"type": "object", "additionalProperties": False}
    registry.register(
        Tool(
            f"{CAP}.read_request",
            CAP,
            SideEffect.READ,
            {**s, "required": ["request_id"], "properties": {"request_id": {"type": "string"}}},
            read_request,
            "Read a service request",
        )
    )
    registry.register(
        Tool(
            f"{CAP}.draft_reply",
            CAP,
            SideEffect.DRAFT,
            {
                **s,
                "required": ["request_id", "subject", "body"],
                "properties": {
                    "request_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
            draft_reply,
            "Save a draft reply",
        )
    )
    registry.register(
        Tool(
            f"{CAP}.send_reply",
            CAP,
            SideEffect.EXTERNAL,
            {
                **s,
                "required": ["to", "subject", "body", "idempotency_key"],
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
            },
            send_reply,
            "Send a reply to the requester",
        )
    )
