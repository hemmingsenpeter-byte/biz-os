"""The inactive roles retain acceptance coverage against a private fake fixture."""

import shutil
from pathlib import Path

import pytest
import yaml

# A test-only record: the real apps/hive stays shadow and carries no authorization.
FIXTURE_AUTHORIZATION = {
    "authorized_by": "fixture-owner",
    "authorized_on": "2026-11-21",
    "pilot_exit_evidence": "fixture: fake ports only",
}


def authorize(target: Path) -> dict:
    config = target / "composition.yaml"
    composition = yaml.safe_load(config.read_text())
    composition["execution_mode"] = "supervised"
    composition["supervised_authorization"] = dict(FIXTURE_AUTHORIZATION)
    config.write_text(yaml.safe_dump(composition))
    return composition


@pytest.fixture
def authorized_hive(tmp_path: Path) -> Path:
    """apps/hive as-is, plus a fixture owner authorization for supervised runs."""
    source = Path(__file__).resolve().parents[2] / "apps/hive"
    target = shutil.copytree(source, tmp_path / "authorized-hive")
    authorize(target)
    return target


@pytest.fixture
def full_hive(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "apps/hive"
    target = shutil.copytree(source, tmp_path / "full-hive")
    config = target / "composition.yaml"
    composition = yaml.safe_load(config.read_text())
    composition["roles"] += ["estimator", "bookkeeper"]
    composition["capabilities"] += ["documents", "voice_capture", "invoicing", "payables", "cash"]
    composition["ports"].update(
        storage="fake_storage",
        sign="fake_sign",
        speech="fake_speech",
        accounting="fake_accounting",
        model_cloud="fake_model",
    )
    composition["execution_mode"] = "supervised"
    composition["supervised_authorization"] = dict(FIXTURE_AUTHORIZATION)
    config.write_text(yaml.safe_dump(composition))
    return target
