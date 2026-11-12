"""The kill switch: one control that pauses every role. See service.py."""

from biz_kernel.control.service import Paused, PauseState, is_paused, pause, resume, state

__all__ = ["PauseState", "Paused", "is_paused", "pause", "resume", "state"]
