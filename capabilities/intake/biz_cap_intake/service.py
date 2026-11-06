"""Intake core: store an enquiry once, publish enquiry.received once, deterministic triage."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from biz_kernel.packets.bus import Bus, Packet
from biz_kernel.records.models import Party, Person
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_intake.models import Enquiry, OptOut

CATEGORIES = ("enquiry", "service_request", "supplier", "spam", "other")
PHONE_CHANNELS = ("phone", "sms")
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out"}
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|auto:|out of office|undeliverable|"
    r"delivery status notification|mail delivery failed|returned mail)",
    re.IGNORECASE,
)
_AUTO_SENDER = re.compile(r"^(mailer-daemon|postmaster|no-?reply|donotreply|do-not-reply)@", re.I)
_SPAM = re.compile(
    r"\b(viagra|casino|crypto giveaway|bitcoin|you have won|claim your prize|backlinks?|"
    r"guaranteed (ranking|traffic)|seo services|click here to claim|earn \$?\d+ (a|per) day|"
    r"lottery|wire transfer|nigerian)\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"https?://", re.IGNORECASE)


@dataclass
class Received:
    enquiry: Enquiry
    packet: Packet | None
    duplicate: bool = False


def is_opt_out_text(text: str) -> bool:
    return text.strip().lower().strip(".!") in STOP_WORDS


def is_opted_out(session: Session, address: str) -> bool:
    return session.get(OptOut, address.strip().lower()) is not None


def add_opt_out(session: Session, address: str, source: str) -> None:
    key = address.strip().lower()
    if session.get(OptOut, key) is None:
        session.add(OptOut(address=key, source=source))
        session.flush()


def classify_rules(channel: str, sender: str, subject: str, body: str) -> dict[str, Any]:
    """Rules before any model. `needs_model` is true only when no rule decided."""
    text = f"{subject}\n{body}"
    if is_opt_out_text(body) or is_opt_out_text(subject):
        return {"category": "other", "needs_model": False, "reason": "opt_out"}
    short_stop = len(text) < 200 and re.search(r"\b(unsubscribe|opt[- ]?out)\b", text, re.I)
    if short_stop or re.search(r"\b(remove me from|take me off)\b", text, re.IGNORECASE):
        return {"category": "other", "needs_model": False, "reason": "opt_out"}
    if _AUTO_SUBJECT.match(subject) or _AUTO_SENDER.match(sender.strip()):
        return {"category": "other", "needs_model": False, "reason": "auto_reply"}
    if _SPAM.search(text) or len(_LINK.findall(body)) >= 4:
        return {"category": "spam", "needs_model": False, "reason": "spam_rule"}
    return {"category": None, "needs_model": True, "reason": None}


def ensure_party(session: Session, enquiry: Enquiry) -> uuid.UUID:
    """Find the party for this contact (by email or phone) or create a person party."""
    if enquiry.party_id is not None:
        return enquiry.party_id
    key = enquiry.sender.strip()
    col = Person.email if "@" in key else Person.phone
    person = session.scalar(select(Person).where(col == key))
    if person is not None and person.party_id is not None:
        enquiry.party_id = person.party_id
    else:
        party = Party(kind="person", name=key)
        session.add(party)
        session.flush()
        session.add(
            Person(
                party_id=party.id,
                full_name=key,
                email=key if "@" in key else None,
                phone=None if "@" in key else key,
            )
        )
        enquiry.party_id = party.id
    session.flush()
    return enquiry.party_id


def _summary(channel: str, sender: str, subject: str, body: str) -> str:
    if channel == "phone":
        return f"Missed call from {sender}"[:200]
    if channel == "sms":
        return f"SMS from {sender}: {body}"[:200]
    return f"{channel}: {subject or body}"[:200]


def receive(
    session: Session,
    bus: Bus,
    *,
    channel: str,
    sender: str,
    subject: str = "",
    body: str = "",
    provider_id: str | None = None,
) -> Received:
    """Store the enquiry (once per provider id) and publish enquiry.received (once).

    An SMS that is an opt-out keyword records the opt-out and publishes nothing.
    """
    enquiry = None
    if provider_id:
        enquiry = session.scalar(
            select(Enquiry).where(Enquiry.channel == channel, Enquiry.provider_id == provider_id)
        )
    duplicate = enquiry is not None
    if enquiry is None:
        enquiry = Enquiry(
            channel=channel, sender=sender, subject=subject, body=body, provider_id=provider_id
        )
        session.add(enquiry)
        session.flush()
    if channel == "sms" and is_opt_out_text(body):
        add_opt_out(session, sender, "sms_stop")
        enquiry.status, enquiry.classified_as, enquiry.reason = "opted_out", "other", "opt_out"
        session.flush()
        return Received(enquiry, None, duplicate)
    can_text = channel in PHONE_CHANNELS and not is_opted_out(session, sender)
    key = f"enquiry.received:{channel}:{provider_id}" if provider_id else None
    packet = bus.publish(
        session,
        type="enquiry.received",
        from_part="intake",
        summary=_summary(channel, sender, subject, body),
        payload={
            "enquiry_id": str(enquiry.id),
            "channel": channel,
            "summary": _summary(channel, sender, subject, body),
            "contact": sender,
            "text_back": "yes" if can_text else "no",
        },
        idempotency_key=key or f"enquiry.received:{enquiry.id}",
    )
    return Received(enquiry, packet, duplicate)
