"""Unit tests: the pure send gate, reply rules, tool schemas."""

from __future__ import annotations

import pytest
from biz_cap_comms import config
from biz_cap_comms.gate import GateConfig, GateInput, normalize_address, run_gate
from biz_cap_comms.service import classify_reply_rules
from biz_cap_comms.tools import register
from biz_kernel.tools.registry import ToolRegistry
from biz_port_fakes import FakeMail, FakeTelephony

CFG = GateConfig(banned_phrases=("Guaranteed",), link_domains=("example.org",))


def _g(body="Hello there", to="a@x.org", channel="mail", cfg=CFG, **kw):  # type: ignore[no-untyped-def]
    return run_gate(
        GateInput(channel=channel, to=to, subject=kw.pop("subject", "Hi"), body=body, **kw), cfg
    )


def test_clean_message_passes() -> None:
    r = _g()
    assert r.passed and r.reasons == ()


@pytest.mark.parametrize("marker", ["{{name}}", "{% if x %}", "[[slot]]", "<UNTRUSTED>x"])
def test_unrendered_tags_block(marker: str) -> None:
    r = _g(body=f"Hi {marker}")
    assert not r.passed and any(x.startswith("unrendered_template") for x in r.reasons)


def test_unrendered_tag_in_subject_blocks() -> None:
    assert not _g(subject="Hi {{name}}").passed


def test_banned_phrase_case_insensitive_and_default_empty() -> None:
    assert "banned_phrase:Guaranteed" in _g(body="this is GUARANTEED").reasons
    assert _g(body="this is GUARANTEED", cfg=GateConfig()).passed


def test_length_per_channel() -> None:
    assert _g(body="x" * 481, to="+6421", channel="sms").reasons == ("too_long",)
    assert _g(body="x" * 481).passed
    assert not _g(body="x" * 21, cfg=GateConfig(max_length={"mail": 20})).passed


def test_links_allow_list() -> None:
    assert _g(body="see https://example.org/a and http://pay.example.org/x").passed
    r = _g(body="see https://evil.test/a")
    assert not r.passed and r.reasons[0].startswith("link_not_allowed")
    assert not _g(body="https://notexample.org/").passed
    assert not _g(body="https://example.org.evil.test/").passed
    assert not _g(body="https://x.test", cfg=GateConfig()).passed  # empty list blocks all


def test_suppression_and_recipient_match() -> None:
    assert "suppressed" in _g(suppressed=True).reasons
    assert "recipient_mismatch" in _g(expected_to="b@x.org").reasons
    assert _g(expected_to="A@X.org").passed
    assert _g(to="+64 21-0", channel="sms", expected_to="+64210").passed


def test_channel_mismatch_and_empty() -> None:
    assert "channel_mismatch" in _g(to="+6421", channel="mail").reasons
    assert "channel_mismatch" in _g(channel="sms").reasons
    assert "empty_body" in _g(body="  ").reasons
    assert "unknown_channel" in _g(channel="fax").reasons


def test_reasons_accumulate() -> None:
    r = _g(body="{{x}} https://evil.test", suppressed=True)
    assert len(r.reasons) == 3


def test_normalize() -> None:
    assert normalize_address(" A@X.ORG ") == "a@x.org"
    assert normalize_address("+64 (21) 0-1") == "+642101"


@pytest.mark.parametrize(
    ("channel", "sender", "subject", "body", "category"),
    [
        ("sms", "+64", "", "STOP", "opt_out"),
        ("mail", "a@x.org", "", "Please unsubscribe me", "opt_out"),
        ("mail", "a@x.org", "", "do not contact me again", "opt_out"),
        ("mail", "mailer-daemon@x.org", "Undeliverable", "x", "auto_reply"),
        ("mail", "a@x.org", "Out of office", "x", "auto_reply"),
        ("mail", "a@x.org", "Quote", "Can you quote a job?", None),
    ],
)
def test_reply_rules(channel, sender, subject, body, category) -> None:  # type: ignore[no-untyped-def]
    out = classify_reply_rules(channel, sender, subject, body)
    assert out["category"] == category and out["needs_model"] is (category is None)


def test_register_takes_gate_config_and_tools_declared() -> None:
    reg = ToolRegistry()
    register(reg, {"mail": FakeMail(), "telephony": FakeTelephony()}, CFG)
    try:
        assert config.gate_config() is CFG
        assert sorted(reg.tools) == [
            "comms.classify_reply",
            "comms.draft",
            "comms.opt_out",
            "comms.queue",
            "comms.reconcile",
            "comms.send",
        ]
    finally:
        config.configure(None)


def test_blocked_message_is_never_sent(session) -> None:  # type: ignore[no-untyped-def]
    mail = FakeMail()
    reg = ToolRegistry()
    register(reg, {"mail": mail, "telephony": FakeTelephony()}, CFG)
    try:
        d = reg.get("comms.draft").fn(
            session=session, channel="mail", to="a@x.org", body="Hi {{name}}", idempotency_key="k"
        )
        q = reg.get("comms.queue").fn(session=session, message_id=d["message_id"])
        assert q["status"] == "blocked"
        with pytest.raises(PermissionError):
            reg.get("comms.send").fn(
                session=session, message_id=d["message_id"], idempotency_key="k"
            )
        assert mail.sent == []
    finally:
        config.configure(None)
