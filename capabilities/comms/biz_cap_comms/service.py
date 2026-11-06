"""Outbox, suppression and reply rules. All deterministic."""

from __future__ import annotations

import re
import uuid
from typing import Any

from biz_kernel.audit.log import record
from biz_kernel.records.models import Person
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_comms.gate import GateConfig, GateInput, GateReport, normalize_address, run_gate
from biz_cap_comms.models import OptOut, Outbox

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out"}
_STOP_PHRASE = re.compile(
    r"\b(remove me from|take me off|do not (contact|email|text) me"
    r"|stop (emailing|texting|messaging) me)\b",
    re.I,
)
_SHORT_STOP = re.compile(r"\b(unsubscribe|opt[- ]?out)\b", re.I)
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|out of office|undeliverable|delivery status)", re.I
)
_AUTO_SENDER = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply)@", re.I)


def is_stop_text(text: str) -> bool:
    return text.strip().lower().strip(".!") in STOP_WORDS


def classify_reply_rules(channel: str, sender: str, subject: str, body: str) -> dict[str, Any]:
    """Opt-out and auto-reply rules. `needs_model` is true only when no rule decided."""
    text = f"{subject}\n{body}"
    if (
        is_stop_text(body)
        or is_stop_text(subject)
        or _STOP_PHRASE.search(text)
        or (len(text) < 200 and _SHORT_STOP.search(text))
    ):
        return {"category": "opt_out", "needs_model": False, "reason": "opt_out"}
    if _AUTO_SUBJECT.match(subject) or _AUTO_SENDER.match(sender.strip()):
        return {"category": "auto_reply", "needs_model": False, "reason": "auto_reply"}
    return {"category": None, "needs_model": True, "reason": None}


def add_opt_out(
    session: Session, address: str, source: str, party_id: uuid.UUID | None = None
) -> OptOut:
    key = normalize_address(address)
    row = session.get(OptOut, key)
    if row is None:
        row = OptOut(address=key, source=source, party_id=party_id)
        session.add(row)
    elif row.party_id is None and party_id is not None:
        row.party_id = party_id
    session.flush()
    if party_id is not None:  # channel-agnostic: every known address of the party
        for person in session.scalars(select(Person).where(Person.party_id == party_id)):
            for other in (person.email, person.phone):
                if other and session.get(OptOut, normalize_address(other)) is None:
                    session.add(
                        OptOut(address=normalize_address(other), party_id=party_id, source=source)
                    )
        session.flush()
    record(session, "comms", "comms.opt_out", key, source=source, party_id=party_id)
    return row


def is_suppressed(session: Session, to: str, party_id: uuid.UUID | None = None) -> bool:
    """Any channel: the same address, or any address of a party that opted out anywhere."""
    if session.get(OptOut, normalize_address(to)) is not None:
        return True
    if party_id is None:
        return False
    return session.scalar(select(OptOut.address).where(OptOut.party_id == party_id)) is not None


def gate_for(session: Session, row: Outbox, cfg: GateConfig) -> GateReport:
    return run_gate(
        GateInput(
            channel=row.channel,
            to=row.to,
            subject=row.subject,
            body=row.body,
            suppressed=is_suppressed(session, row.to, row.party_id),
            expected_to=row.expected_to,
        ),
        cfg,
    )
