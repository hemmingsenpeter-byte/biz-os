"""Import every kernel module that defines tables so Base.metadata is complete."""

from __future__ import annotations

import importlib
import pkgutil

import biz_kernel


def import_all_models() -> None:
    for mod in pkgutil.walk_packages(biz_kernel.__path__, "biz_kernel."):
        if mod.name.rsplit(".", 1)[-1] in ("models", "log", "bus", "store", "tables"):
            importlib.import_module(mod.name)
    # Port adapters (separate packages) own their ledger tables in biz_port_*.models.
    for mod in pkgutil.iter_modules():
        if mod.name.startswith("biz_port_"):
            try:
                importlib.import_module(f"{mod.name}.models")
            except ModuleNotFoundError as exc:
                if exc.name != f"{mod.name}.models":
                    raise
