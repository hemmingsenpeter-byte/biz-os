"""The inactive roles retain acceptance coverage against a private fake fixture."""

import shutil
from pathlib import Path

import pytest
import yaml


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
    config.write_text(yaml.safe_dump(composition))
    return target
