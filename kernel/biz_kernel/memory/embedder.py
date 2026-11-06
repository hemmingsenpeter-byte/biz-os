"""Embedder protocol and a deterministic hashing embedder for tests and the demo."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from biz_kernel.memory.models import EMBED_DIM

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Hashing trick over lower-cased tokens, L2-normalised. Same text, same vector."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
            vec[h % self.dim] += -1.0 if (h >> 63) & 1 else 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]
