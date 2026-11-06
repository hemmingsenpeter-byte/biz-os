"""intake: turns calls, forms and emails into enquiries, triaged by rules before any model."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from biz_cap_intake import config
from biz_cap_intake.models import Cursor, Enquiry, OptOut, SentText
from biz_cap_intake.tools import register

__all__ = ["Cursor", "Enquiry", "OptOut", "SentText", "pollers", "register", "routes"]


def routes(rt: Any) -> Any:
    """Hook: the router mounted at /webhooks (public; /v1 stays private)."""
    from biz_cap_intake.web import build_router

    config.bind(rt.manifest)
    return build_router(rt)


def pollers(rt: Any) -> list[Callable[[], int]]:
    """Hook: called once when the runtime is built; binds composition config, returns pollers."""
    from biz_cap_intake.web import poll_mail

    config.bind(rt.manifest)
    return [lambda: poll_mail(rt)]
