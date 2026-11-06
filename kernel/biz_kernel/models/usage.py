"""Token usage and cost reported by model adapters, passed to the router via a side channel."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = Decimal(0)

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cost_usd + other.cost_usd,
        )


# Set by an adapter at the end of `structured`; read (and cleared) by the router.
last_usage: ContextVar[Usage | None] = ContextVar("biz_model_last_usage", default=None)


def report(usage: Usage) -> None:
    last_usage.set(usage)


def take() -> Usage:
    """Usage of the call just made (zero if the adapter reported none)."""
    u = last_usage.get()
    last_usage.set(None)
    return u or Usage()
