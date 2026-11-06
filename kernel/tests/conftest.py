"""API/auth fixtures: a TestClient bound to the rolled-back test session and a fake mail port."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from biz_kernel.api.app import create_app
from biz_kernel.auth.deps import get_session
from biz_port_fakes import FakeMail


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIZ_SECRET_KEY", "test-secret-not-real")


@pytest.fixture
def mail() -> FakeMail:
    return FakeMail()


@pytest.fixture
def client(session: Session, mail: FakeMail) -> Iterator[TestClient]:
    app = create_app(mail=mail)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
