"""Every port protocol has a fake that satisfies it. Skipped until kernel ports exist."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest


def _protocols() -> list[type]:
    try:
        pkg = importlib.import_module("biz_kernel.ports")
    except ModuleNotFoundError:
        return []
    out = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        m = importlib.import_module(f"biz_kernel.ports.{mod.name}")
        out += [
            c
            for _, c in inspect.getmembers(m, inspect.isclass)
            if getattr(c, "_is_protocol", False)
            and c.__module__ == m.__name__
            and c.__name__.endswith("Port")
        ]
    return out


PROTOCOLS = _protocols()


@pytest.mark.skipif(not PROTOCOLS, reason="no port protocols in biz_kernel.ports yet")
@pytest.mark.parametrize("proto", PROTOCOLS or [object], ids=lambda p: p.__name__)
def test_fake_implements_protocol(proto: type) -> None:
    fakes = importlib.import_module("biz_port_fakes")
    methods = {n for n, v in vars(proto).items() if callable(v) and not n.startswith("_")}
    impls = [
        c
        for _, c in inspect.getmembers(fakes, inspect.isclass)
        if methods and methods <= {n for n in dir(c) if not n.startswith("_")}
    ]
    assert impls, f"no fake in biz_port_fakes provides {sorted(methods)} for {proto.__name__}"
