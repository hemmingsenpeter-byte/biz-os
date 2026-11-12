"""Intake core: store an enquiry once, publish enquiry.received once, deterministic triage."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from biz_kernel import db
from biz_kernel.packets.bus import Bus, Packet
from biz_kernel.policy.types import Deny
from biz_kernel.records.models import Party, Person
from biz_kernel.tools.registry import ToolRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_cap_intake import config
from biz_cap_intake.models import Enquiry, OptOut, SentText

CATEGORIES = ("enquiry", "service_request", "supplier", "spam", "other")
# Whole-message keywords (the entire message, ignoring case and punctuation).
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out"}
# A short message (at most MAX_OPT_OUT_WORDS words) that starts with one of these also opts out.
STOP_STARTS = ("stop", "stopall", "unsubscribe", "optout", "opt out", "remove me", "take me off")
MAX_OPT_OUT_WORDS = 4
TEXT_BACK_WINDOW = timedelta(hours=24)
MAX_SENDER, MAX_PROVIDER_ID, MAX_SUBJECT, MAX_BODY = 320, 200, 500, 20000
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


def _words(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    words = cleaned.split()
    if words and words[0] == "please":
        words = words[1:]
    return " ".join(words)


def is_opt_out_text(text: str) -> bool:
    """True only for a whole-message keyword, or a short message that starts with one.

    A long message that merely mentions a phrase (a quote request that says "take me off the
    other list") is not an opt-out.
    """
    norm = _words(text)
    if not norm:
        return False
    if norm in STOP_WORDS:
        return True
    if len(norm.split()) > MAX_OPT_OUT_WORDS:
        return False
    return any(norm == p or norm.startswith(p + " ") for p in STOP_STARTS)


def normalize_phone(raw: str, default_country_code: str | None = None) -> str:
    """Digits only (with a leading + kept). A leading national 0 becomes the configured
    default country code; without one the number is left as digits."""
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return text
    if text.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    cc = re.sub(r"\D", "", default_country_code or "")
    if cc and digits.startswith("0"):
        return f"+{cc}{digits[1:]}"
    return digits


def normalize_address(raw: str) -> str:
    """Lowercased, trimmed email, or a normalised phone number."""
    text = raw.strip()
    if "@" in text:
        return text.lower()
    cc = config.setting("default_country_code", None)
    return normalize_phone(text, str(cc) if cc else None)


SHARED_CHECK, SHARED_RECORD = "comms.is_suppressed", "comms.opt_out"


class _NoGate:
    """Policy for the two shared-list calls: neither tool is gated, so nothing is allowed."""

    def check(self, session: Session, action: Any) -> Deny:
        return Deny("intake calls only ungated tools")


def _shared(session: Session, registry: ToolRegistry | None, name: str, **args: Any) -> Any:
    """Ask the shared suppression list through its published tool, when it is loaded.

    There is one opt-out list for the whole business (owned by comms). Intake keeps its own
    rows as well, so it still honours opt-outs in a composition without comms.
    """
    if registry is None or name not in registry.tools:
        return None
    return registry.call(session, _NoGate(), actor="intake", name=name, args=args).output


def is_opted_out(session: Session, address: str, registry: ToolRegistry | None = None) -> bool:
    key = normalize_address(address)
    if session.get(OptOut, key) is not None:
        return True
    out = _shared(session, registry, SHARED_CHECK, address=key)
    return bool(out and out.get("suppressed"))


def add_opt_out(
    session: Session, address: str, source: str, registry: ToolRegistry | None = None
) -> None:
    key = normalize_address(address)
    if session.get(OptOut, key) is None:
        session.add(OptOut(address=key, source=source))
        session.flush()
    _shared(session, registry, SHARED_RECORD, address=key, source=source[:40])


def recently_texted(session: Session, number: str, exclude_key: str | None = None) -> bool:
    """True when a text-back already went to this number within the throttle window."""
    q = select(SentText.key).where(
        SentText.to == normalize_address(number),
        SentText.at >= db.utcnow() - TEXT_BACK_WINDOW,
    )
    if exclude_key:
        q = q.where(SentText.key != exclude_key)
    return session.scalar(q.limit(1)) is not None


def classify_rules(channel: str, sender: str, subject: str, body: str) -> dict[str, Any]:
    """Rules before any model. `needs_model` is true only when no rule decided."""
    text = f"{subject}\n{body}"
    if is_opt_out_text(subject) or is_opt_out_text(body):
        return {"category": "other", "needs_model": False, "reason": "opt_out"}
    if _AUTO_SUBJECT.match(subject) or _AUTO_SENDER.match(sender.strip()):
        return {"category": "other", "needs_model": False, "reason": "auto_reply"}
    if _SPAM.search(text) or len(_LINK.findall(body)) >= 4:
        return {"category": "spam", "needs_model": False, "reason": "spam_rule"}
    return {"category": None, "needs_model": True, "reason": None}


def ensure_party(session: Session, enquiry: Enquiry) -> uuid.UUID:
    """The party for this enquiry. A claimed email or phone is rung 0 (unverified): it never
    links to an existing person. A new, contact-less party is made; the claim stays on the
    enquiry (claimed_email / claimed_phone, verified=false) until a person is verified."""
    if enquiry.party_id is not None:
        return enquiry.party_id
    party = Party(kind="person", name=f"Unverified contact {enquiry.sender}"[:200])
    session.add(party)
    session.flush()
    session.add(Person(party_id=party.id, full_name=f"Unverified contact {enquiry.sender}"[:200]))
    enquiry.party_id = party.id
    session.flush()
    return party.id


def _summary(channel: str, sender: str, subject: str, body: str) -> str:
    if channel == "phone":
        return f"Missed call from {sender} (unverified contact)"[:200]
    if channel == "sms":
        return f"SMS from {sender} (unverified contact): {body}"[:200]
    return f"{channel} (unverified contact {sender}): {subject or body}"[:200]


def receive(
    session: Session,
    bus: Bus,
    *,
    channel: str,
    sender: str,
    subject: str = "",
    body: str = "",
    provider_id: str | None = None,
    registry: ToolRegistry | None = None,
) -> Received:
    """Store the enquiry (once per provider id) and publish enquiry.received (once).

    An SMS that is an opt-out keyword records the opt-out and publishes nothing.
    """
    sender = normalize_address(sender)[:MAX_SENDER]
    subject, body = subject[:MAX_SUBJECT], body[:MAX_BODY]
    provider_id = provider_id[:MAX_PROVIDER_ID] if provider_id else None
    enquiry = None
    if provider_id:
        enquiry = session.scalar(
            select(Enquiry).where(Enquiry.channel == channel, Enquiry.provider_id == provider_id)
        )
    duplicate = enquiry is not None
    if enquiry is None:
        enquiry = Enquiry(
            channel=channel,
            sender=sender,
            subject=subject,
            body=body,
            provider_id=provider_id,
            claimed_email=sender if "@" in sender else None,
            claimed_phone=None if "@" in sender else sender,
            verified=False,
        )
        session.add(enquiry)
        session.flush()
    if channel == "sms" and is_opt_out_text(body):
        add_opt_out(session, sender, "sms_stop", registry)
        enquiry.status, enquiry.classified_as, enquiry.reason = "opted_out", "other", "opt_out"
        session.flush()
        return Received(enquiry, None, duplicate)
    # Only a missed call earns the templated text-back, at most once per number per 24h.
    can_text = (
        channel == "phone"
        and not is_opted_out(session, sender, registry)
        and not recently_texted(session, sender)
    )
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
            "verified": False,
            "text_back": "yes" if can_text else "no",
        },
        idempotency_key=key or f"enquiry.received:{enquiry.id}",
    )
    return Received(enquiry, packet, duplicate)
