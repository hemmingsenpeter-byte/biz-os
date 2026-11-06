"""voice_capture: voice notes to transcripts, structured notes and tasks on the right job."""

from __future__ import annotations

from typing import Any

from biz_cap_voice_capture.models import VoiceNote
from biz_cap_voice_capture.tools import register

__all__ = ["VoiceNote", "owner_routes", "register"]


def owner_routes(rt: Any) -> Any:
    """Hook: owner-authenticated router for /v1 (POST /voice_capture/upload).

    The kernel mounts `routes(rt)` publicly under /webhooks; this capability has no public
    routes, so the composition must mount this one under /v1.
    """
    from biz_cap_voice_capture.web import build_owner_router

    return build_owner_router(rt)
