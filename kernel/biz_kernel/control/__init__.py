"""The kill switch: one control that pauses every role. See service.py."""

from biz_kernel.control.service import (
    Paused,
    PauseState,
    guard,
    is_paused,
    pause,
    resume,
    state,
)

__all__ = ["PauseState", "Paused", "guard", "is_paused", "pause", "resume", "state"]
