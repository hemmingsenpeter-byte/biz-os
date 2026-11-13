"""Every role's golden case folder loads, and its labels agree with its own checks."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pytest
from biz_kernel.models.eval import Case, load_cases, score_case

ROOT = Path(__file__).resolve().parents[3]
FOLDERS = sorted(
    p.parent for p in ROOT.glob("roles/*/tests/eval/*/*.yaml") if not p.name.startswith("_")
)
FOLDERS = sorted(set(FOLDERS))
HELDOUT = [p for p in FOLDERS if p.name.endswith("_heldout")]
# Only exact and numeric checks can be answered from `expected` alone.
ANSWERABLE = {"json_schema", "exact", "numeric"}


def _id(p: Path) -> str:
    return str(p.relative_to(ROOT))


def _ideal(c: Case) -> dict[str, Any]:
    """An answer a perfect model would give: the labels, plus plausible required fields."""
    out = dict(c.expected)
    for name in c.schema.get("required", []):
        if name in out:
            continue
        kind = c.schema["properties"][name].get("type")
        out[name] = 0.85 if kind == "number" else "text"
    return out


def _bodies(folder: Path) -> dict[str, str]:
    out = {}
    for c in load_cases(folder):
        for u in c.untrusted:
            data = json.loads(u)
            msg = data.get("reply") or data.get("enquiry") or {}
            out[c.name] = str(msg.get("body", ""))
    return out


def test_case_folders_found():
    assert len(FOLDERS) >= 4 and HELDOUT


@pytest.mark.parametrize("folder", FOLDERS, ids=_id)
def test_folder_loads(folder: Path):
    cases = load_cases(folder)
    assert cases
    names = [c.name for c in cases]
    assert len(names) == len(set(names)), "duplicate case names"
    assert {f.stem for f in folder.glob("*.yaml") if not f.name.startswith("_")} == set(names)
    for c in cases:
        assert c.schema and (c.messages or c.untrusted), c.name
        for u in c.untrusted:
            if u.lstrip().startswith("{"):
                json.loads(u)  # the JSON the workflow runner would hand the draft step


@pytest.mark.parametrize("folder", FOLDERS, ids=_id)
def test_labels_agree_with_checks(folder: Path):
    for c in load_cases(folder):
        if not c.expected or {chk["type"] for chk in c.checks} - ANSWERABLE:
            continue
        res = score_case(c, _ideal(c))
        assert res.passed, (c.name, res.failures)


@pytest.mark.parametrize("heldout", HELDOUT, ids=_id)
def test_heldout_split_is_big_enough_and_not_copied(heldout: Path):
    tuning = heldout.with_name(heldout.name.removesuffix("_heldout"))
    assert len(load_cases(heldout)) >= 50
    assert (heldout / "_schema.yaml").read_text() == (tuning / "_schema.yaml").read_text()
    held, tuned = _bodies(heldout), _bodies(tuning)
    close = [
        (h, t)
        for h, hb in held.items()
        for t, tb in tuned.items()
        if SequenceMatcher(None, hb.lower(), tb.lower()).ratio() > 0.8
    ]
    assert not close, close
