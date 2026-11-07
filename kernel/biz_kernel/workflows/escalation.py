"""Role `escalate_when` conditions: parsed strictly, evaluated generically, fail closed.

Two forms exist. `{confidence_below: N}` reads the `confidence` of a draft step's output;
a missing or non-numeric confidence counts as a match. A bare name is a signal: it matches
when a step output carries it in its `signals` list. Signal names are declared by
capabilities (`signals:` in capability.yaml); nothing here knows what they mean.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

CONFIDENCE = "confidence_below"


class ConditionError(ValueError):
    """An escalate_when entry is unknown or malformed."""


@dataclass(frozen=True)
class Condition:
    name: str
    threshold: float | None = None


def parse(raw: Any, *, known_signals: Collection[str]) -> tuple[Condition, ...]:
    if not isinstance(raw, list):
        raise ConditionError(f"escalate_when must be a list, got {raw!r}")
    out: list[Condition] = []
    for item in raw:
        if isinstance(item, str):
            if item == CONFIDENCE or item not in known_signals:
                raise ConditionError(f"unknown escalate_when condition {item!r}")
            out.append(Condition(item))
        elif isinstance(item, dict) and len(item) == 1 and CONFIDENCE in item:
            v = item[CONFIDENCE]
            if isinstance(v, bool) or not isinstance(v, int | float) or not 0 <= v <= 1:
                raise ConditionError(f"{CONFIDENCE} needs a number from 0 to 1, got {v!r}")
            out.append(Condition(CONFIDENCE, float(v)))
        else:
            raise ConditionError(f"unknown escalate_when condition {item!r}")
    return tuple(out)


def check(conds: Collection[Condition], *, kind: str, output: Any) -> str | None:
    """Return the reason the first matching condition gives, or None."""
    for c in conds:
        if c.name == CONFIDENCE:
            if kind != "draft":
                continue
            conf = output.get("confidence") if isinstance(output, dict) else None
            ok = (
                isinstance(conf, int | float)
                and not isinstance(conf, bool)
                and not math.isnan(conf)
                and c.threshold is not None
                and conf >= c.threshold
            )
            if not ok:
                return f"confidence {conf!r} is below {c.threshold} (or missing)"
        elif isinstance(output, dict):
            sigs = output.get("signals")
            if isinstance(sigs, list) and c.name in sigs:
                return f"condition {c.name} met"
    return None
