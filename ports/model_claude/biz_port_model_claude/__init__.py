"""Claude model adapter: tool-use structured output, prompt caching, cost in USD."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Literal

import anthropic
from biz_kernel.models import usage as usage_mod

from .config import (
    CACHE_READ_FACTOR,
    CACHE_WRITE_FACTOR,
    DEFAULT_MODEL,
    PRICES_USD_PER_MTOK,
)

__all__ = ["ClaudeModel", "create", "cost_usd"]

TOOL_NAME = "emit_result"


def cost_usd(model: str, u: usage_mod.Usage) -> Decimal:
    p_in, p_out = PRICES_USD_PER_MTOK.get(model, PRICES_USD_PER_MTOK["default"])
    total = (
        u.input_tokens * p_in
        + u.output_tokens * p_out
        + u.cache_read_tokens * p_in * CACHE_READ_FACTOR
        + u.cache_write_tokens * p_in * CACHE_WRITE_FACTOR
    ) / 1_000_000
    return Decimal(str(round(total, 8)))


class ClaudeModel:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.client = client or anthropic.AsyncAnthropic()
        self.last_usage = usage_mod.Usage()

    @classmethod
    def create(cls) -> ClaudeModel:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return cls(
            model=os.environ.get("BIZ_CLAUDE_MODEL", DEFAULT_MODEL),
            client=anthropic.AsyncAnthropic(api_key=key),
        )

    async def structured(
        self,
        system: str,
        messages: list[Any],
        schema: dict[str, Any],
        tier: Literal["local", "cloud"] = "cloud",
    ) -> dict[str, Any]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "Return the result as structured data.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        u = resp.usage
        self.last_usage = usage_mod.Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        self.last_usage.cost_usd = cost_usd(self.model, self.last_usage)
        usage_mod.report(self.last_usage)
        for block in resp.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                return dict(block.input)  # type: ignore[arg-type]
        raise ValueError("model returned no tool_use block")


def create() -> ClaudeModel:
    """Entry point used by the runtime port resolver."""
    return ClaudeModel.create()
