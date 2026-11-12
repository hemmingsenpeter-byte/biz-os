"""Keep evaluation fixtures aligned with production prompts, inputs and rule routing."""

import json
from pathlib import Path

import pytest
import yaml
from biz_cap_comms.service import classify_reply_rules
from biz_cap_comms.tools import MIN_CONFIDENCE
from biz_cap_intake.service import classify_rules
from biz_cap_intake.tools import LOW_CONFIDENCE

ROOT = Path(__file__).resolve().parents[3]
ROLE = ROOT / "roles/receptionist"
CASES = ROLE / "tests/eval"


@pytest.mark.parametrize("directory", ["triage", "triage_heldout", "handle_reply"])
def test_fixtures_use_the_production_contract(directory: str) -> None:
    task = "handle_reply" if directory == "handle_reply" else "triage"
    sop = yaml.safe_load((ROLE / f"sops/{task}.yaml").read_text())
    step = next(step for step in sop["steps"] if step["kind"] == "draft")
    files = [p for p in sorted((CASES / directory).glob("*.yaml")) if not p.name.startswith("_")]
    assert files
    for path in files:
        case = yaml.safe_load(path.read_text())
        assert yaml.safe_load((path.parent / case["schema_file"]).read_text()) == step["schema"]
        assert (path.parent / case["system_file"]).resolve() == (ROLE / step["prompt"]).resolve()
        assert len(case["untrusted"]) == 1
        data = json.loads(case["untrusted"][0])
        assert json.dumps(data, indent=2, default=str) == case["untrusted"][0], path.name
        assert set(data) == set(step["inputs"]), path.name
        expected = case["expected"]
        if task == "triage":
            enquiry = data["enquiry"]
            assert set(enquiry) == {"channel", "from", "subject", "body"}
            assert classify_rules(
                enquiry["channel"], enquiry["from"], enquiry["subject"], enquiry["body"]
            )["needs_model"], path.name
            minimum = (
                LOW_CONFIDENCE if expected["category"] in {"enquiry", "service_request"} else None
            )
        else:
            reply = data["reply"]
            assert set(reply) == {"subject", "body"}
            assert classify_reply_rules(
                "mail", "fixture@example.invalid", reply["subject"], reply["body"]
            )["needs_model"], path.name
            indices = [slot["index"] for slot in data["offered_slots"]]
            assert indices == list(range(len(indices)))
            if expected["intent"] == "choose_slot":
                assert expected["slot_index"] in indices, path.name
                minimum = MIN_CONFIDENCE
            else:
                assert expected["slot_index"] is None, path.name
                minimum = None
        checks = [check for check in case["checks"] if check.get("field") == "confidence"]
        assert checks == (
            []
            if minimum is None
            else [{"type": "numeric_min", "field": "confidence", "min": minimum}]
        ), path.name
