"""comms: outbox, deterministic send gate, reply rules and permanent opt-outs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from biz_cap_comms import config, service
from biz_cap_comms.gate import GateConfig, GateInput, GateReport, run_gate
from biz_cap_comms.models import OptOut, Outbox
from biz_cap_comms.tools import register

__all__ = [
    "GateConfig",
    "GateInput",
    "GateReport",
    "OptOut",
    "Outbox",
    "handle_packet",
    "pollers",
    "register",
    "run_gate",
]

STOP_PACKETS = ("reply.received", "enquiry.received")


def handle_packet(session: Any, packet_type: str, payload: dict[str, Any]) -> bool:
    """Packet consumer: stop keywords on a reply or enquiry record an opt-out. True if recorded."""
    if packet_type not in STOP_PACKETS:
        return False
    sender = payload.get("sender") or payload.get("contact") or ""
    text = payload.get("body") or payload.get("summary") or ""
    if not sender:
        return False
    out = service.classify_reply_rules(
        payload.get("channel", ""), sender, payload.get("subject", ""), text
    )
    if out["reason"] != "opt_out":
        return False
    pid = payload.get("party_id")
    service.add_opt_out(session, sender, packet_type, uuid.UUID(pid) if pid else None)
    return True


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds composition config. No pollers (intake polls inbound mail)."""
    config.bind(rt.manifest)
    return []
