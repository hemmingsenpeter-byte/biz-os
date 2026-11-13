"""Parse RFC 8601 `Authentication-Results` headers into a SenderAuth verdict.

Only a header stamped by our own receiving server is trusted: its authserv-id must equal the
configured one. The receiving server prepends its header, so the topmost header carrying our
authserv-id wins; a header the sender wrote lower down cannot override it. With no configured
authserv-id nothing is trusted and the message stays unverified.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from biz_kernel.contracts.ports import AuthVerdict, SenderAuth

HEADER = "authentication-results"
MAX_HEADER = 8192  # longer is not a header a mail server wrote; ignore it


@dataclass
class Parsed:
    authserv_id: str
    results: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)


def _strip_comments(text: str) -> str:
    """Drop (comments), which may nest; keep quoted strings intact."""
    out: list[str] = []
    depth, quoted, escaped = 0, False, False
    for ch in text:
        if escaped:
            if depth == 0:
                out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
            if depth == 0:
                out.append(ch)
        elif quoted:
            out.append(ch)
            quoted = ch != '"'
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            out.append(" ")
        elif depth == 0:
            out.append(ch)
            quoted = ch == '"'
    return "".join(out)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _resinfo(segment: str) -> tuple[str, str, dict[str, str]] | None:
    tokens = segment.split()
    if not tokens or "=" not in tokens[0]:
        return None
    method, result = tokens[0].split("=", 1)
    props: dict[str, str] = {}
    for tok in tokens[1:]:
        if "=" in tok:
            key, val = tok.split("=", 1)
            props.setdefault(key.lower(), _unquote(val))
    return method.split("/")[0].lower(), _unquote(result).lower(), props


def parse(value: str) -> Parsed | None:
    """One header value. The authserv-id is "" when the header has none (it starts with a
    result). None for a value that is not a usable Authentication-Results header."""
    if not value or len(value) > MAX_HEADER:
        return None
    segments = _strip_comments(" ".join(value.split())).split(";")
    head = segments[0].split()
    if not head:
        return None
    if "=" in head[0]:
        parsed, rest = Parsed(authserv_id=""), segments
    else:
        parsed, rest = Parsed(authserv_id=_unquote(head[0]).lower()), segments[1:]
        if len(head) > 2:
            return None  # authserv-id, optional version, then only results
    for seg in rest:
        if seg.strip().lower() == "none":
            continue  # no-result
        info = _resinfo(seg)
        if info is not None:
            parsed.results.append(info)
    return parsed


def _verdict(results: list[str]) -> AuthVerdict:
    """pass only if something passed and nothing else failed; none if nothing was checked."""
    checked = [r for r in results if r != "none"]
    if not checked:
        return "none"
    return "pass" if all(r == "pass" for r in checked) else "fail"


def to_sender_auth(parsed: Parsed) -> SenderAuth:
    by: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for method, result, props in parsed.results:
        by.setdefault(method, []).append((result, props))
    dmarc = by.get("dmarc", [])
    from_domains = {p.get("header.from", "").strip().rstrip(".").lower() for _, p in dmarc}
    dmarc_verdict = _verdict([r for r, _ in dmarc])
    if len(from_domains) != 1 or "" in from_domains:
        dmarc_verdict = "fail" if dmarc_verdict == "pass" else dmarc_verdict
    dkim = [r for r, _ in by.get("dkim", [])]
    return SenderAuth(
        dmarc=dmarc_verdict,
        spf=_verdict([r for r, _ in by.get("spf", [])]),
        # One passing signature is enough for DKIM; DMARC decides alignment.
        dkim="pass" if "pass" in dkim else _verdict(dkim),
        from_domain=next(iter(from_domains)) if len(from_domains) == 1 else "",
        authserv_id=parsed.authserv_id,
    )


def trusted_auth(headers: Iterable[Any], authserv_id: str) -> SenderAuth:
    """The verdict from the topmost Authentication-Results header our server stamped.

    `headers` are Graph internetMessageHeaders ({"name", "value"}) in message order, top
    first. Unverified when `authserv_id` is empty or no header carries it."""
    trusted = authserv_id.strip().lower()
    if not trusted:
        return SenderAuth()
    for h in headers or []:
        if not isinstance(h, dict) or str(h.get("name", "")).strip().lower() != HEADER:
            continue
        parsed = parse(str(h.get("value") or ""))
        if parsed is not None and parsed.authserv_id and parsed.authserv_id == trusted:
            return to_sender_auth(parsed)
    return SenderAuth()
