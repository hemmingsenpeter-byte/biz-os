"""The guest probe cannot turn incomplete or same-boot evidence into acceptance."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from deploy import virtual_acceptance as acceptance


@pytest.mark.parametrize("bad_guard", [False, True])
def test_http_probe_accepts_hydrated_login_shell_but_keeps_auth_guards(monkeypatch, bad_guard):
    monkeypatch.setenv("BIZ_RP_ID", "localhost")

    def respond(request):
        path = request.url.path
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/auth/me":
            return httpx.Response(401, json={"detail": "sign in"})
        if path == "/auth/login":
            return httpx.Response(200, text="<!doctype html><html>Loading<script></script></html>")
        if path == "/v1/auth/passkey/register/begin":
            return httpx.Response(200 if bad_guard else 401, json={})
        if path == "/v1/auth/passkey/login/begin":
            return httpx.Response(200, json={"options": {"rpId": "localhost"}})
        raise AssertionError(path)

    original = httpx.Client
    monkeypatch.setattr(
        acceptance.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    if bad_guard:
        with pytest.raises(RuntimeError, match="unauthenticated caller"):
            acceptance.http_checks()
    else:
        result = acceptance.http_checks()
        assert result["registration_guard"] is True
        assert result["passkey_ceremony"] == "not exercised by this command"


@pytest.mark.parametrize("failure", [RuntimeError("probe failed"), KeyboardInterrupt()])
def test_partial_guest_run_replaces_stale_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException
) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"status":"PASS"}')
    monkeypatch.setattr(
        sys,
        "argv",
        ["virtual-acceptance", "--out", str(report), "--state-dir", str(tmp_path / "state")],
    )
    monkeypatch.setattr(acceptance, "load_environment", lambda _: None)

    def failed(*_: object) -> None:
        assert json.loads(report.read_text())["status"] == "RUNNING"
        raise failure

    monkeypatch.setattr(acceptance, "execute", failed)
    with pytest.raises(type(failure)):
        acceptance.main()
    result = json.loads(report.read_text())
    assert result["status"] == "UNVERIFIED" and result["error_type"] == type(failure).__name__
    assert report.stat().st_mode & 0o777 == 0o600


def test_reboot_requires_a_different_kernel_boot_and_same_source() -> None:
    old = str(uuid.uuid4())
    current = str(uuid.uuid4())
    checkpoint = {"boot_id": old, "source_sha256": "a" * 64, "phase": "awaiting_reboot"}
    with pytest.raises(RuntimeError, match="has not rebooted"):
        acceptance.verify_reboot(checkpoint, old.upper(), "a" * 64)
    with pytest.raises(RuntimeError, match="source changed"):
        acceptance.verify_reboot(checkpoint, current, "b" * 64)
    with pytest.raises(RuntimeError, match="complete pre-reboot"):
        acceptance.verify_reboot({**checkpoint, "phase": "preparing"}, current, "a" * 64)
    acceptance.verify_reboot(checkpoint, current, "a" * 64)


def test_source_binding_includes_prompts_but_ignores_installed_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    prompt = tmp_path / "roles" / "fixture" / "prompts" / "draft.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Original prompt")
    dependency = tmp_path / ".venv" / "dependency.py"
    dependency.parent.mkdir()
    dependency.write_text("Original package")
    original = acceptance.source_sha256()
    dependency.write_text("Rebuilt package")
    assert acceptance.source_sha256() == original
    prompt.write_text("Changed prompt")
    assert acceptance.source_sha256() != original


def test_backup_failure_restarts_dependent_console_and_comparison_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("BIZ_DATABASE_URL", "postgresql+psycopg://fixture@127.0.0.1/biz")
    monkeypatch.setenv("BIZ_DBOS_SYSTEM_URL", "postgresql+psycopg://fixture@127.0.0.1/biz_dbos")
    calls = []

    def run(arguments, *args):
        calls.append(arguments)
        if Path(arguments[0]).name == "pg_dump":
            raise RuntimeError("synthetic dump failure")
        return ""

    monkeypatch.setattr(acceptance, "command", run)
    with pytest.raises(RuntimeError, match="synthetic dump failure"):
        acceptance.backup_marker(tmp_path, {}, 1)
    expected = {
        "biz-api.service",
        "biz-worker.service",
        "biz-web.service",
        "biz-comparison.service",
    }
    assert calls[0][:2] == ["systemctl", "stop"] and set(calls[0][2:]) == expected
    assert calls[-1][:2] == ["systemctl", "start"] and set(calls[-1][2:]) == expected


def test_source_change_during_recovery_does_not_promote_the_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkpoint = {
        "boot_id": str(uuid.uuid4()),
        "source_sha256": "a" * 64,
        "phase": "awaiting_reboot",
        "marker": {},
        "backup": {"database_dumps": []},
        "probe": {},
        "smoke": {"passed": True},
    }
    acceptance.write_json(tmp_path / "checkpoint.json", checkpoint)
    source_hashes = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(acceptance, "source_sha256", lambda: next(source_hashes))
    monkeypatch.setattr(acceptance, "boot_id", lambda: str(uuid.uuid4()))
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Linux")
    monkeypatch.setattr(acceptance.os, "geteuid", lambda: 0)
    monkeypatch.delenv("PYTHONOPTIMIZE", raising=False)
    monkeypatch.setenv("BIZ_DATABASE_URL", "postgresql+psycopg://fixture@127.0.0.1/fixture")
    monkeypatch.setattr(acceptance, "fake_shadow_configuration", lambda: {})
    monkeypatch.setattr(acceptance, "service_state", lambda: {})
    monkeypatch.setattr(acceptance, "http_checks", lambda: {})
    monkeypatch.setattr(acceptance.db, "configure", lambda _: None)
    monkeypatch.setattr(acceptance.db, "engine", Mock(return_value=Mock()))
    monkeypatch.setattr(acceptance, "database_checks", lambda _: {})
    monkeypatch.setattr(acceptance, "verify_marker", lambda *_: None)
    monkeypatch.setattr(acceptance, "resume_probe", lambda *_: {"fake_send_invocations": 1})
    with pytest.raises(RuntimeError, match="source changed during reboot"):
        acceptance.execute(tmp_path, True, 1)
    assert json.loads((tmp_path / "checkpoint.json").read_text())["phase"] == "awaiting_reboot"
