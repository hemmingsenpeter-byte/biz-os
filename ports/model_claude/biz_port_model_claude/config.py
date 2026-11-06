"""Claude adapter settings: price table (USD per million tokens). Edit when prices change."""

from __future__ import annotations

DEFAULT_MODEL = "claude-sonnet-5-5"

# model -> (input, output); cache read = 0.1x input, cache write = 1.25x input.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (3.0, 15.0),
    "default": (3.0, 15.0),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25
