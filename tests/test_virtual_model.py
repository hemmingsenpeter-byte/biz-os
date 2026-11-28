"""The virtual model bootstrap publishes only verified, bounded CPU runtime bytes."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from deploy import virtual_model as model


def archive(entries: list[tuple[str, bytes | str]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if isinstance(value, str):
                member.type, member.linkname = tarfile.SYMTYPE, value
                tar.addfile(member)
            else:
                member.size, member.mode = len(value), 0o755
                tar.addfile(member, io.BytesIO(value))
    return output.getvalue()


def test_only_cpu_root_files_are_retained(tmp_path: Path) -> None:
    payload = archive(
        [
            ("bin/ollama", b"binary"),
            ("lib/ollama/libcpu.so.1", b"CPU"),
            ("lib/ollama/libcpu.so", "libcpu.so.1"),
            ("lib/ollama/cuda_v12/libgpu.so", b"GPU"),
            ("lib/ollama/vulkan/libgpu.so", b"GPU"),
        ]
    )
    report = model.extract_cpu(io.BytesIO(payload), tmp_path)
    assert report["retained_bytes"] == 9
    assert (tmp_path / "lib/ollama/libcpu.so").read_bytes() == b"CPU"
    assert not (tmp_path / "lib/ollama/cuda_v12").exists()
    assert report["files"]["bin/ollama"] == hashlib.sha256(b"binary").hexdigest()


@pytest.mark.parametrize(
    "name", ["../../escape", "/bin/ollama", "lib/ollama/../../escape", "lib/ollama/..\\escape"]
)
def test_archive_paths_cannot_escape_staging(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        model.extract_cpu(io.BytesIO(archive([(name, b"bad")])), tmp_path)


@pytest.mark.parametrize("target", ["/etc/passwd", "../../etc/passwd", "../libcpu.so"])
def test_cpu_links_stay_in_their_directory(tmp_path: Path, target: str) -> None:
    payload = archive([("bin/ollama", b"binary"), ("lib/ollama/libcpu.so", target)])
    with pytest.raises(ValueError, match="symlink"):
        model.extract_cpu(io.BytesIO(payload), tmp_path)


def test_retained_cpu_budget_is_enforced_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disk budget"):
        model.extract_cpu(io.BytesIO(archive([("bin/ollama", b"binary")])), tmp_path, limit=2)
    assert not (tmp_path / "bin/ollama").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="streaming runtime bootstrap needs zstd")
def test_whole_archive_must_match_before_runtime_is_published(tmp_path: Path, monkeypatch) -> None:
    tar = archive([("bin/ollama", b"not executable before verification")])
    compressed = subprocess.check_output(["zstd", "-c"], input=tar)
    monkeypatch.setattr(model, "_request", lambda _: io.BytesIO(compressed))
    monkeypatch.setattr(model, "RUNTIME_BYTES", len(compressed))
    monkeypatch.setattr(model, "RUNTIME_SHA256", "0" * 64)
    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="whole-file verification"):
        model.stage_runtime(destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".ollama-unverified-*"))
