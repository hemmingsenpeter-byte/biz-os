"""documents: templates to PDF, versioned documents, e-signature requests and webhooks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_documents import config
from biz_cap_documents.models import Render, Signature
from biz_cap_documents.tools import register

__all__ = ["Render", "Signature", "pollers", "register", "routes"]


def routes(rt: Any) -> Any:
    """Hook: the router mounted at /webhooks (public; every request is verified)."""
    from biz_cap_documents.web import build_router

    config.bind(rt.manifest)
    return build_router(rt)


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: binds composition config (template slot fills) for the tools; nothing to poll."""
    config.bind(rt.manifest)
    return []
