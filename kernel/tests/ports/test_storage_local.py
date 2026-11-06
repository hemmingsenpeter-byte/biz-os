"""LocalStorage: contract suite, idempotent puts, refusal of paths outside the root."""

from __future__ import annotations

from pathlib import Path

import pytest
from biz_kernel.contracts import suite
from biz_port_storage_local import LocalStorage, create


def test_passes_contract_suite(tmp_path: Path) -> None:
    suite.check_storage_port(LocalStorage(tmp_path / "root"))


def test_put_is_idempotent_and_content_addressed(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path)
    a = st.put(b"hello", "a.pdf", "application/pdf")
    b = st.put(b"hello", "b.pdf", "application/pdf")
    assert a == b and a.uri.startswith("file://") and a.uri.endswith(".pdf")
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_get_refuses_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    st = LocalStorage(root)
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    for uri in (
        secret.as_uri(),
        (root / ".." / "secret.txt").as_uri(),
        root.as_uri(),
        "http://example.com/x",
    ):
        with pytest.raises(PermissionError):
            st.get(uri)
        assert not st.exists(uri)


def test_get_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    st = LocalStorage(root)
    (tmp_path / "secret").write_text("x")
    (root / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(PermissionError):
        st.get((root / "link").as_uri())


def test_get_detects_corruption(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path)
    obj = st.put(b"data", "x.bin", "application/octet-stream")
    Path(obj.uri.removeprefix("file://")).write_bytes(b"tampered")
    with pytest.raises(OSError):
        st.get(obj.uri)


def test_create_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIZ_STORAGE_ROOT", str(tmp_path / "r"))
    assert create().root == (tmp_path / "r").resolve()
