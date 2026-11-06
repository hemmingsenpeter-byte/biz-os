"""Every packet schema is valid; every role's packets and golden inputs fit their schemas."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from biz_kernel.loader import load_composition
from biz_kernel.packets.bus import SchemaError, SchemaRegistry

ROOT = Path(__file__).resolve().parents[2]
CORE = [
    "enquiry.received",
    "enquiry.declined",
    "opportunity.qualified",
    "meeting.booked",
    "brief.confirmed",
    "survey.completed",
    "scope.approved",
    "estimate.ready",
    "proposal.ready",
    "proposal.sent",
    "proposal.accepted",
    "contract.signed",
    "programme.baseline",
    "booking.confirmed",
    "po.issued",
    "goods.received",
    "site.report",
    "site.issue",
    "variation.raised",
    "variation.approved",
    "milestone.reached",
    "invoice.issued",
    "payment.received",
    "bill.matched",
    "practical_completion",
    "job.handed_over",
    "service_request.received",
    "escalation.raised",
    "task.created",
    "message.sent",
    "note.captured",
    "brief.daily",
    "variation.priced",
    "escalation.assigned",
]


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    r = SchemaRegistry()
    r.load_dir(ROOT / "packets")
    return r


def test_core_packet_set_present(registry: SchemaRegistry) -> None:
    known = {t for t, _ in registry.known()}
    assert not set(CORE) - known


def test_schema_ids_match_paths() -> None:
    import json

    for p in (ROOT / "packets").glob("*/v*.json"):
        assert json.loads(p.read_text())["$id"] == f"{p.parent.name}/{p.stem}"


def test_invalid_payload_rejected(registry: SchemaRegistry) -> None:
    with pytest.raises(SchemaError):
        registry.validate("message.sent", 1, {"channel": "mail"})


@pytest.mark.parametrize("app", ["demo", "hive"])
def test_roles_use_known_packets_and_goldens_validate(app: str, registry: SchemaRegistry) -> None:
    m = load_composition(ROOT / "apps" / app, ROOT)
    for role in m.roles.values():
        for t in [*role.consumes, *role.publishes]:
            registry.latest(t)
        for g in sorted((role.path / "tests" / "golden").glob("*.yaml")):
            case = yaml.safe_load(g.read_text())
            pkt = case["input"]["packet"]
            registry.validate(pkt["type"], registry.latest(pkt["type"]), pkt["payload"])
