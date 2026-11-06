"""Local filesystem adapter for StoragePort: content-addressed files under one root."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from biz_kernel.contracts.ports import StoredObject

__all__ = ["LocalStorage", "create"]

_EXT = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class LocalStorage:
    """Files live at <root>/<sha[:2]>/<sha><ext>. Same bytes, same file: put is idempotent."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, name: str, content_type: str) -> StoredObject:
        sha = hashlib.sha256(data).hexdigest()
        # Only a short, sanitised extension of the caller's name is kept; the path is the hash.
        ext = Path(name).suffix.lower()
        ext = ext if _EXT.match(ext) else ""
        target = self.root / sha[:2] / f"{sha}{ext}"
        existing = next(target.parent.glob(f"{sha}.*"), None) if target.parent.exists() else None
        if existing is None and target.exists():
            existing = target
        if existing is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            existing = target
        return StoredObject(uri=existing.as_uri(), sha256=sha, size=len(data))

    def _path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise PermissionError(f"not a local file uri: {uri!r}")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise PermissionError("path is outside the storage root")
        return path

    def get(self, uri: str) -> bytes:
        path = self._path(uri)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != path.name.split(".", 1)[0]:
            raise OSError(f"stored object {path.name} does not match its hash")
        return data

    def exists(self, uri: str) -> bool:
        try:
            return self._path(uri).is_file()
        except PermissionError:
            return False


def create() -> LocalStorage:
    """Build from BIZ_STORAGE_ROOT (required)."""
    return LocalStorage(os.environ["BIZ_STORAGE_ROOT"])
