"""Alembic migrations: a `kernel` branch plus one `pack_<name>` branch per installed pack."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config

HERE = Path(__file__).parent
KERNEL_VERSIONS = HERE / "versions" / "kernel"


def pack_branch(pack_name: str) -> str:
    """Branch label for a pack's migrations."""
    return f"pack_{pack_name}"


def make_config(url: str, pack_dirs: Sequence[Path] = ()) -> Config:
    """Programmatic Alembic config; `pack_dirs` are extra version directories (one per pack)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    locations = [str(KERNEL_VERSIONS), *(str(p) for p in pack_dirs)]
    cfg.set_main_option("version_locations", " ".join(locations))
    cfg.set_main_option("path_separator", "space")
    cfg.attributes["url"] = url
    return cfg


def upgrade(url: str, revision: str = "kernel@head", pack_dirs: Sequence[Path] = ()) -> None:
    command.upgrade(make_config(url, pack_dirs), revision)


def downgrade(
    url: str,
    revision: str = "base",
    pack_dirs: Sequence[Path] = (),
    *,
    allow_data_loss: bool = False,
) -> None:
    config = make_config(url, pack_dirs)
    config.attributes["allow_data_loss"] = allow_data_loss
    command.downgrade(config, revision)
