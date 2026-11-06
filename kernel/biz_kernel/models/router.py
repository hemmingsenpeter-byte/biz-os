"""The only place model calls happen. M0: tier choice and schema checks; M1 adds full rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import jsonschema

Tier = Literal["local", "cloud"]


class ModelPort(Protocol):
    async def structured(self, system: str, messages: list, schema: dict, tier: Tier) -> dict: ...


def fence(text: str) -> str:
    """Untrusted text (emails, web pages, transcripts, packet bodies) is always fenced."""
    cleaned = str(text).replace("<untrusted>", "").replace("</untrusted>", "")
    return f"<untrusted>\n{cleaned}\n</untrusted>"


@dataclass
class RoutedResult:
    output: dict[str, Any]
    tier: Tier
    attempts: int = 1


@dataclass
class Router:
    local: ModelPort
    cloud: ModelPort | None = None
    task_tiers: dict[str, Tier] = field(default_factory=dict)
    high_stakes: set[str] = field(default_factory=set)

    def pick(self, task: str, tier_hint: Tier | None = None) -> Tier:
        if tier_hint:
            return tier_hint
        if task in self.high_stakes and self.cloud is not None:
            return "cloud"
        return self.task_tiers.get(task, "local")

    async def generate(
        self,
        task: str,
        system: str,
        messages: list,
        schema: dict[str, Any],
        tier_hint: Tier | None = None,
    ) -> RoutedResult:
        tier = self.pick(task, tier_hint)
        port = self.cloud if tier == "cloud" and self.cloud else self.local
        last: Exception | None = None
        for attempt in (1, 2):
            out = await port.structured(system, messages, schema, tier)
            try:
                jsonschema.validate(out, schema)
                return RoutedResult(out, tier, attempt)
            except jsonschema.ValidationError as exc:
                last = exc
        if self.cloud is not None and tier == "local":
            out = await self.cloud.structured(system, messages, schema, "cloud")
            jsonschema.validate(out, schema)
            return RoutedResult(out, "cloud", 3)
        raise ValueError(f"model output failed schema for {task}: {last}")


def load_prompt(path: Path) -> str:
    return path.read_text()
