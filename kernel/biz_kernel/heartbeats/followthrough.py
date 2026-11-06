"""Follow-through registry: modules register finders for unfinished multi-step work."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

Find = Callable[[Session], list[Any]]
Resume = Callable[[Session, Any], None]


class FollowThrough:
    def __init__(self) -> None:
        self._kinds: dict[str, tuple[Find, Resume]] = {}

    def register(self, kind: str, find: Find, resume: Resume) -> None:
        self._kinds[kind] = (find, resume)

    def sweep(self, session: Session) -> int:
        """Resume each unfinished item once. A failing item never blocks the rest."""
        resumed = 0
        for kind, (find, resume) in self._kinds.items():
            for ref in find(session):
                try:
                    with session.begin_nested():
                        resume(session, ref)
                    resumed += 1
                except Exception:
                    log.exception("follow-through %s failed for %r", kind, ref)
        return resumed
