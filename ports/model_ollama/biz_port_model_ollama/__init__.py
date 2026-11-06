"""Ollama model adapter: structured JSON output and embeddings, local cost is zero."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal

import httpx
from biz_kernel.models import usage as usage_mod

__all__ = ["OllamaModel", "create"]

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


class OllamaModel:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        *,
        embed_model: str = DEFAULT_EMBED_MODEL,
        temperature: float = 0.1,
        max_concurrency: int = 2,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.temperature = temperature
        self.max_concurrency = max_concurrency
        self.timeout = timeout
        self._client = client
        self._sems: dict[int, asyncio.Semaphore] = {}
        self.last_usage = usage_mod.Usage()

    @classmethod
    def create(cls) -> OllamaModel:
        return cls(
            base_url=os.environ.get("BIZ_OLLAMA_URL", DEFAULT_URL),
            model=os.environ.get("BIZ_OLLAMA_MODEL", DEFAULT_MODEL),
            embed_model=os.environ.get("BIZ_OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        )

    def _sem(self) -> asyncio.Semaphore:
        # One semaphore per running loop (the SOP runner uses a fresh loop per step).
        loop_id = id(asyncio.get_running_loop())
        return self._sems.setdefault(loop_id, asyncio.Semaphore(self.max_concurrency))

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self._sem():
            if self._client is not None:
                resp = await self._client.post(self.base_url + path, json=body)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    resp = await c.post(self.base_url + path, json=body)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def structured(
        self,
        system: str,
        messages: list[Any],
        schema: dict[str, Any],
        tier: Literal["local", "cloud"] = "local",
    ) -> dict[str, Any]:
        chat = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
            chat.append({"role": m["role"], "content": content})
        data = await self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": chat,
                "format": schema,
                "stream": False,
                "options": {"temperature": self.temperature},
            },
        )
        self.last_usage = usage_mod.Usage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
        usage_mod.report(self.last_usage)
        out = json.loads(data["message"]["content"])
        if not isinstance(out, dict):
            raise ValueError("model returned non-object JSON")
        return out

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await self._post("/api/embed", {"model": self.embed_model, "input": texts})
        return [list(map(float, v)) for v in data["embeddings"]]


def create() -> OllamaModel:
    """Entry point used by the runtime port resolver."""
    return OllamaModel.create()
