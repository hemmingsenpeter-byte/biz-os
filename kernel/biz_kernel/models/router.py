"""The only place model calls happen: tier choice, validation, escalation, budget, fencing."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import jsonschema
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.budgets.service import BudgetBook
from biz_kernel.models import usage as usage_mod

Tier = Literal["local", "cloud"]
Rule = Callable[[dict[str, Any]], str | None]

_TAG = re.compile(r"<\s*/?\s*untrusted\b[^>]*>?", re.IGNORECASE)
_BLOCK = re.compile(r"<untrusted>.*?</untrusted>", re.DOTALL)


class ModelPort(Protocol):
    async def structured(self, system: str, messages: list, schema: dict, tier: Tier) -> dict: ...


class UntrustedNotFenced(ValueError):
    """A prompt contained untrusted text outside <untrusted> tags."""


def fence(text: str) -> str:
    """Untrusted text (emails, web pages, transcripts, packet bodies) is always fenced.

    Any nested or forged tag (any case, any spacing) is removed first, repeatedly.
    """
    cleaned = str(text)
    while True:
        nxt = _TAG.sub("", cleaned)
        if nxt == cleaned:
            break
        cleaned = nxt
    return f"<untrusted>\n{cleaned}\n</untrusted>"


def _text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _check_fenced(messages: list, system: str, untrusted: list[str]) -> None:
    """Refuse a prompt in which any untrusted text sits outside an <untrusted> block."""
    parts = [system] + [_text(m.get("content", "")) for m in messages]
    for part in parts:
        outside = _BLOCK.sub("", part)
        for text in untrusted:
            probe = _TAG.sub("", str(text)).strip()
            if probe and probe in outside:
                raise UntrustedNotFenced("untrusted text found outside <untrusted> tags")


@dataclass
class RoutedResult:
    output: dict[str, Any]
    tier: Tier
    attempts: int = 1
    escalated: list[str] = field(default_factory=list)
    flagged: bool = False
    usage: usage_mod.Usage = field(default_factory=usage_mod.Usage)

    @property
    def cost_usd(self) -> Decimal:
        return self.usage.cost_usd


@dataclass
class Router:
    local: ModelPort
    cloud: ModelPort | None = None
    task_tiers: dict[str, Tier] = field(default_factory=dict)
    high_stakes: set[str] = field(default_factory=set)
    budgets: BudgetBook | None = None
    confidence_threshold: float = 0.5

    def pick(self, task: str, tier_hint: Tier | None = None) -> Tier:
        if tier_hint:
            return tier_hint
        default = self.task_tiers.get(task, "local")  # rule 1
        # rule 2: the list may name the full task ("estimator.pricing") or the topic ("pricing")
        topic = task.rsplit(".", 1)[-1]
        if (task in self.high_stakes or topic in self.high_stakes) and self.cloud is not None:
            return "cloud"
        return default

    async def generate(
        self,
        task: str,
        system: str,
        messages: list,
        schema: dict[str, Any],
        tier_hint: Tier | None = None,
        *,
        untrusted: list[str] | None = None,
        rules: list[Rule] | None = None,
        session: Session | None = None,
        role: str | None = None,
        escalate: bool = True,
        now: datetime | None = None,
    ) -> RoutedResult:
        now = now or datetime.now(UTC)
        messages = list(messages)
        if untrusted:
            messages.append({"role": "user", "content": "\n\n".join(fence(t) for t in untrusted)})
            _check_fenced(messages, system, untrusted)
        result = RoutedResult({}, "local", 0)
        book = self.budgets or BudgetBook({})
        metered = session is not None and role is not None

        def cloud_allowed() -> bool:
            if self.cloud is None:
                return False
            # Estimate the next call from this task's costliest call this month, so a call
            # that would take the role over its limit is never started.
            if metered and not book.can_afford(
                session,  # type: ignore[arg-type]
                role,  # type: ignore[arg-type]
                book.estimate(session, role, task, now),  # type: ignore[arg-type]
                now,
            ):
                result.flagged = True  # rule 5: stay local, owner reviews
                return False
            return True

        async def call(tier: Tier) -> dict[str, Any]:
            port = self.cloud if tier == "cloud" and self.cloud else self.local
            usage_mod.take()
            out = await port.structured(system, messages, schema, tier)
            used = usage_mod.take()
            if tier == "local":
                used.cost_usd = Decimal(0)
            result.usage = result.usage + used
            result.attempts += 1
            if metered and used.cost_usd > 0:  # free (local) calls leave no ledger row
                # The call has returned, so its cost is real: always record it.
                book.charge(session, role, used.cost_usd, "model", task, now, incurred=True)  # type: ignore[arg-type]
                if not book.can_spend(session, role, now):  # type: ignore[arg-type]
                    result.flagged = True
            return out

        def problem(out: dict[str, Any]) -> str | None:
            try:
                jsonschema.validate(out, schema)
            except jsonschema.ValidationError as exc:
                return f"schema: {exc.message}"
            for rule in rules or []:
                msg = rule(out)
                if msg:
                    return f"rule: {msg}"
            return None

        def low_confidence(out: dict[str, Any]) -> bool:
            c = out.get("confidence")
            return isinstance(c, int | float) and c < self.confidence_threshold

        tier = self.pick(task, tier_hint)
        if tier == "cloud" and not cloud_allowed():
            tier = "local"
        result.tier = tier
        last_problem: str | None = None
        best: dict[str, Any] | None = None

        for attempt in range(2):
            if attempt and tier == "cloud" and not cloud_allowed():
                result.flagged = True  # budget reached mid-task: no further paid call
                break
            out = await call(tier)
            last_problem = problem(out)
            if last_problem is None:
                result.output, result.tier = out, tier
                if tier == "local" and escalate and low_confidence(out):
                    last_problem = "low confidence"
                    best = out
                    break
                return self._done(result, session, role, task)
            if tier == "cloud":
                continue
        if tier == "cloud" or not escalate:
            if best is not None:
                return self._done(result, session, role, task)
            raise ValueError(f"model output failed validation for {task}: {last_problem}")

        reason = f"{task}: local {last_problem}"
        if cloud_allowed():
            result.escalated.append(reason)
            if session is not None:
                record(session, role or "router", "model.escalated", task, reason=last_problem)
            for attempt in range(2):
                if attempt and not cloud_allowed():
                    result.flagged = True
                    break
                out = await call("cloud")
                last_problem = problem(out)
                if last_problem is None:
                    result.output, result.tier = out, "cloud"
                    return self._done(result, session, role, task)
            raise ValueError(f"model output failed validation for {task}: {last_problem}")
        if best is not None:  # budget reached or no cloud: keep the local answer, flagged
            result.flagged = True
            return self._done(result, session, role, task)
        result.flagged = True
        raise ValueError(f"model output failed validation for {task}: {last_problem}")

    def _done(
        self, result: RoutedResult, session: Session | None, role: str | None, task: str
    ) -> RoutedResult:
        if result.flagged and session is not None:
            record(session, role or "router", "model.flagged", task, reason="review needed")
        return result


def load_prompt(path: Path) -> str:
    return path.read_text()
