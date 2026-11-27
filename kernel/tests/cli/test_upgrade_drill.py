"""An unsuccessful upgrade attempt must not leave earlier passing evidence behind."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from deploy import upgrade_drill


@pytest.mark.parametrize("failure", [RuntimeError("worker failed"), KeyboardInterrupt()])
def test_failed_attempt_replaces_old_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException
) -> None:
    output = tmp_path / "evidence.json"
    output.write_text('{"status":"PASS"}')
    monkeypatch.setattr(sys, "argv", ["upgrade-drill", "--out", str(output)])

    def interrupted() -> None:
        assert json.loads(output.read_text())["status"] == "RUNNING"
        raise failure

    monkeypatch.setattr(upgrade_drill, "drill", interrupted)
    with pytest.raises(type(failure)):
        upgrade_drill.main()
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "UNVERIFIED"
    assert evidence["error"] == (
        "interrupted" if isinstance(failure, KeyboardInterrupt) else "worker failed"
    )
    assert output.stat().st_mode & 0o777 == 0o600
