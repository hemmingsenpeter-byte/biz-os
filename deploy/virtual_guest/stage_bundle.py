"""Stream prepared, locked build artifacts into the native Linux appliance layout."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

EXCLUDED = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".next",
    ".cache",
    "test-results",
    "playwright-report",
}
SOURCE_TOP = {
    "README.md",
    "CLAUDE.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "kernel",
    "capabilities",
    "ports",
    "packets",
    "roles",
    "industries",
    "apps",
    "deploy",
    "docs",
    "tests",
    "conftest.py",
    "LICENSE",
    "NOTICE",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def entries(root: Path, excluded: set[str] | None = None) -> Iterator[Path]:
    excluded = excluded or set()
    yield root
    if root.is_dir() and not root.is_symlink():
        for child in sorted(root.iterdir()):
            if (
                child.name in excluded
                or child.name == ".env"
                or (child.name.startswith(".env.") and not child.name.endswith(".example"))
            ):
                continue
            if child.suffix in {".pyc", ".pyo"}:
                continue
            yield from entries(child, excluded)


def source_commit(source: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()


def runtime_prefix(venv: Path) -> Path:
    return (venv / "bin/python").resolve().parent.parent


def build_plan(
    source: Path, venv: Path, python_root: Path, node: Path, web: Path
) -> list[tuple[Path, str]]:
    planned = []
    for child in sorted(source.iterdir()):
        if child.name in SOURCE_TOP:
            for path in entries(child, EXCLUDED):
                planned.append((path, "opt/biz-os/" + str(path.relative_to(source))))
    for path in entries(venv, {"__pycache__"}):
        planned.append(
            (path, "opt/biz-os/.venv" + ("/" + str(path.relative_to(venv)) if path != venv else ""))
        )
    for name in ["python", "python3", "python3.12"]:
        planned.append((python_root / "bin" / name, "opt/biz-python/bin/" + name))
    for path in sorted((python_root / "lib").glob("libpython*.so*")):
        planned.append((path, "opt/biz-python/lib/" + path.name))
    stdlib = python_root / "lib/python3.12"
    for path in entries(stdlib, {"site-packages", "__pycache__"}):
        planned.append(
            (
                path,
                "opt/biz-python/lib/python3.12"
                + ("/" + str(path.relative_to(stdlib)) if path != stdlib else ""),
            )
        )
    planned.append((node, "opt/biz-node/bin/node"))
    standalone = web / ".next/standalone"
    for path in entries(standalone, {"__pycache__"}):
        planned.append(
            (
                path,
                "opt/biz-web"
                + ("/" + str(path.relative_to(standalone)) if path != standalone else ""),
            )
        )
    # Next's standalone output does not necessarily contain its static/public assets.
    for relative in [Path(".next/static"), Path("public")]:
        origin = web / relative
        if origin.exists():
            for path in entries(origin):
                planned.append((path, "opt/biz-web/" + str(relative / path.relative_to(origin))))
    # Prefer the explicitly supplied assets when standalone already contains them.
    return list({target: (path, target) for path, target in planned}.values())


def rewrite(path: Path, target: str, substitutions: dict[str, str]) -> bytes | None:
    if target.endswith("/.venv/pyvenv.cfg"):
        return (
            b"home = /opt/biz-python/bin\nimplementation = CPython\n"
            b"version_info = 3.12\ninclude-system-site-packages = false\n"
        )
    if not path.is_file() or path.is_symlink():
        return None
    editable = path.suffix in {".pth", ".egg-link"} or path.name.startswith("_editable")
    script = "/.venv/bin/" in target and path.stat().st_size < 100_000
    direct_url = path.name == "direct_url.json" and "/.venv/" in target
    if not (editable or script or direct_url):
        return None
    raw = path.read_bytes()
    if b"\0" in raw:
        return None
    for original, replacement in sorted(substitutions.items(), key=lambda pair: -len(pair[0])):
        raw = raw.replace(original.encode(), replacement.encode())
    return raw


def add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.mode, info.size, info.mtime = 0o644, len(content), 0
    archive.addfile(info, io.BytesIO(content))


def write_bundle(
    output: Path,
    plan: list[tuple[Path, str]],
    manifest: dict[str, Any],
    substitutions: dict[str, str],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bundle-", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with (
            os.fdopen(descriptor, "wb") as stream,
            gzip.GzipFile(
                filename="", mode="wb", compresslevel=3, fileobj=stream, mtime=0
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
        ):
            for path, target in plan:
                if target in {
                    "opt/biz-os/SOURCE_COMMIT",
                    "opt/biz-os/deploy/virtual_guest/bundle-manifest.json",
                }:
                    continue
                info = archive.gettarinfo(str(path), arcname=target)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = "root"
                info.mode = 0o755 if info.isdir() or path.stat().st_mode & 0o111 else 0o644
                if info.issym():
                    info.mode = 0o777
                    if target == "opt/biz-os/.venv/bin/python":
                        info.linkname = "/opt/biz-python/bin/python3.12"
                    else:
                        for original, replacement in substitutions.items():
                            if info.linkname.startswith(original):
                                info.linkname = info.linkname.replace(original, replacement, 1)
                                break
                    archive.addfile(info)
                elif info.isfile():
                    rewritten = rewrite(path, target, substitutions)
                    if rewritten is not None:
                        info.size = len(rewritten)
                        archive.addfile(info, io.BytesIO(rewritten))
                    else:
                        with path.open("rb") as content:
                            archive.addfile(info, content)
                else:
                    archive.addfile(info)
            add_bytes(
                archive, "opt/biz-os/SOURCE_COMMIT", (manifest["source_commit"] + "\n").encode()
            )
            add_bytes(
                archive,
                "opt/biz-os/deploy/virtual_guest/bundle-manifest.json",
                (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--python-root", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--web-source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    venv = (args.venv or source / ".venv").resolve()
    python_root = (args.python_root or runtime_prefix(venv)).resolve()
    node_path = args.node or shutil.which("node")
    if node_path is None:
        parser.error("--node is required when node is absent from PATH")
    node = Path(node_path).resolve()
    web = (args.web_source or source / "web").resolve()
    required = [
        source / "uv.lock",
        venv / "pyvenv.cfg",
        python_root / "bin/python3.12",
        node,
        web / ".next/standalone/server.js",
        venv / "lib/python3.12/site-packages/pgserver/pginstall/bin/postgres",
    ]
    for path in required:
        if not path.is_file():
            parser.error(f"required artifact missing: {path}")
    venv_lock = venv.parent / "uv.lock"
    if not venv_lock.is_file() or digest(venv_lock) != digest(source / "uv.lock"):
        parser.error("prepared environment must come from a project with the same uv.lock")
    manifest = {
        "format": 1,
        "source_commit": source_commit(source),
        "source_root": str(source),
        "venv_root": str(venv),
        "python_root": str(python_root),
        "uv_lock_sha256": digest(source / "uv.lock"),
        "venv_lock_sha256": digest(venv_lock),
        "python_sha256": digest(python_root / "bin/python3.12"),
        "node_sha256": digest(node),
        "web_build_id": (web / ".next/BUILD_ID").read_text().strip(),
        "layout": {
            "source": "/opt/biz-os",
            "python": "/opt/biz-python",
            "node": "/opt/biz-node/bin/node",
            "web": "/opt/biz-web",
        },
    }
    substitutions = {
        str(venv): "/opt/biz-os/.venv",
        str(venv.parent): "/opt/biz-os",
        str(source): "/opt/biz-os",
        str(python_root): "/opt/biz-python",
    }
    plan = build_plan(source, venv, python_root, node, web)
    total = sum(path.stat().st_size for path, _ in plan if path.is_file() and not path.is_symlink())
    if args.dry_run:
        print(
            json.dumps(
                {**manifest, "archive_entries": len(plan), "uncompressed_bytes": total}, indent=2
            )
        )
        return 0
    if args.output is None:
        parser.error("--output is required without --dry-run")
    output = args.output.resolve()
    write_bundle(output, plan, manifest, substitutions)
    print(
        json.dumps(
            {
                "output": str(output),
                "bytes": output.stat().st_size,
                "sha256": digest(output),
                "archive_entries": len(plan),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
