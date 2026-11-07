"""The production app (api/main.build_app) wires the composition's mail port like telephony."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from biz_port_fakes import FakeMail

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def prod_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    import dbos
    from biz_kernel.api import main

    monkeypatch.setenv("BIZ_APP_DIR", str(ROOT / "apps" / "demo"))
    monkeypatch.setenv("BIZ_REPO_ROOT", str(ROOT))
    monkeypatch.setenv("BIZ_SECRET_KEY", "test-secret-not-real")
    monkeypatch.setattr(dbos, "DBOSClient", lambda **kw: object())
    return main.build_app()


def test_production_app_exposes_the_mail_port(prod_app: Any) -> None:
    assert isinstance(prod_app.state.mail, FakeMail)


def test_mail_wiring_keeps_v1_private_and_webhooks_public(prod_app: Any) -> None:
    paths = set(prod_app.openapi()["paths"])
    assert "/webhooks/telephony" in paths
    # Mail is a port, not a route: public means /webhooks only, and nothing mail-shaped there.
    public = {p for p in paths if not p.startswith(("/v1", "/webhooks"))}
    assert public <= {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    assert not any(p.startswith("/webhooks") and "mail" in p for p in paths)
