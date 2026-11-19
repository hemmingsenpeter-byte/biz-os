"""Run mode: `live`, or `shadow` (practice).

In shadow mode the roles read real inbound work and draft as usual, but nothing with an
outside effect ever runs: no send, no booking, no posting, no payment, approved or not.
Each action a role would have taken is recorded as a shadow proposal instead, so the
owner can compare it with what they did themselves (docs/PLAN.md, Stage 1). The mode is
set in policy.yaml (`mode: shadow`); going live is an edit to that file.
"""

from __future__ import annotations

from typing import Any

LIVE, SHADOW = "live", "shadow"
MODES = (LIVE, SHADOW)


class ShadowMode(PermissionError):
    """Raised instead of running an outside-effect tool while the composition is in shadow."""

    def __init__(self, what: str = "") -> None:
        self.what = what
        super().__init__(f"shadow mode: {what} not run" if what else "shadow mode")

    def __reduce__(self) -> tuple[type[ShadowMode], tuple[str]]:
        return (ShadowMode, (self.what,))


def mode_of(policy: dict[str, Any] | None) -> str:
    """The configured mode; `live` when unset. Validate with `mode_problems` first."""
    value = (policy or {}).get("mode", LIVE)
    return value if value in MODES else SHADOW  # anything unrecognised fails safe


def mode_problems(policy: dict[str, Any] | None) -> list[str]:
    value = (policy or {}).get("mode", LIVE)
    if value not in MODES:
        return [f"mode must be one of {', '.join(MODES)}, not {value!r}"]
    return []
