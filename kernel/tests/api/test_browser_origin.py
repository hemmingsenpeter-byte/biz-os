"""Cookie-authenticated API mutations reject cross-origin browser requests at Caddy too."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://other.example.com"},
        {"Origin": "https://workspace.example.com:444"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://workspace.example.com", "Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_mutation_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    monkeypatch.setenv("BIZ_ORIGIN", "https://workspace.example.com")
    client.cookies.set("biz_session", "browser-session")
    response = client.post("/v1/auth/logout", headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://workspace.example.com"},
        {"Origin": "https://workspace.example.com", "Sec-Fetch-Site": "same-origin"},
    ],
)
def test_same_origin_browser_and_headerless_client_keep_working(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    monkeypatch.setenv("BIZ_ORIGIN", "https://workspace.example.com/")
    response = client.post("/v1/auth/logout", headers=headers)
    assert response.status_code == 204
    assert "set-cookie" in response.headers


def test_cross_origin_reads_and_webhook_paths_are_not_browser_mutations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIZ_ORIGIN", "https://workspace.example.com")
    headers = {"Origin": "https://other.example.com", "Sec-Fetch-Site": "cross-site"}
    assert client.get("/v1/health", headers=headers).status_code == 200
    assert client.post("/webhooks/not-installed", headers=headers).status_code == 404
