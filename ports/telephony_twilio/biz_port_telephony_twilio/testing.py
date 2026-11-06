"""Test helpers: a fake Twilio client and a signed-webhook builder."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from twilio.request_validator import RequestValidator


class FakeTwilioClient:
    """Stands in for twilio.rest.Client. Set `error` to raise from create()."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return SimpleNamespace(sid=f"SM{len(self.sent):032d}")


def signed_request(
    token: str, url: str, form: dict[str, str], signature: str | None = None
) -> dict[str, Any]:
    sig = signature or RequestValidator(token).compute_signature(url, form)
    return {"form": form, "headers": {"X-Twilio-Signature": sig}, "url": url}
