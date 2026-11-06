"""Deterministic send gate: a pure function, no model, no I/O. Failing messages are never sent."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

TEMPLATE_MARKERS = ("{{", "{%", "[[", "<untrusted>")
_LINK = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
DEFAULT_MAX_LENGTH = {"mail": 20000, "sms": 480}


@dataclass(frozen=True)
class GateConfig:
    banned_phrases: tuple[str, ...] = ()
    link_domains: tuple[str, ...] = ()
    max_length: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_LENGTH))


@dataclass(frozen=True)
class GateInput:
    channel: str
    to: str
    subject: str
    body: str
    suppressed: bool = False
    expected_to: str | None = None  # recipient recorded on the source message, when given


@dataclass(frozen=True)
class GateReport:
    passed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "reasons": list(self.reasons)}


def normalize_address(address: str) -> str:
    """Case-fold addresses; strip punctuation from phone numbers."""
    a = address.strip().lower()
    return a if "@" in a else re.sub(r"[\s\-().]", "", a)


def _link_allowed(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def run_gate(msg: GateInput, cfg: GateConfig) -> GateReport:
    reasons: list[str] = []
    text = f"{msg.subject}\n{msg.body}"
    low = text.lower()
    if msg.channel not in ("mail", "sms"):
        reasons.append("unknown_channel")
    elif ("@" in msg.to) != (msg.channel == "mail"):
        reasons.append("channel_mismatch")
    if not msg.body.strip():
        reasons.append("empty_body")
    for marker in TEMPLATE_MARKERS:
        if marker in low:
            reasons.append(f"unrendered_template:{marker}")
    for phrase in cfg.banned_phrases:
        if phrase and phrase.lower() in low:
            reasons.append(f"banned_phrase:{phrase}")
    limit = cfg.max_length.get(msg.channel)
    if limit is not None and len(msg.body) > limit:
        reasons.append("too_long")
    for url in _LINK.findall(text):
        if not _link_allowed(url, cfg.link_domains):
            reasons.append(f"link_not_allowed:{url}")
    if msg.suppressed:
        reasons.append("suppressed")
    if msg.expected_to is not None and normalize_address(msg.expected_to) != normalize_address(
        msg.to
    ):
        reasons.append("recipient_mismatch")
    return GateReport(not reasons, tuple(reasons))
