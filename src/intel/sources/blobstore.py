"""Object-store port for raw captures (spec 03 §3 blobs).

The fetch workflow writes bytes here and inserts the blob row in the
same commit transaction; the object key is content-addressed so the
owner-scoped UNIQUE(owner_id, sha256, media_type) dedup maps 1:1 onto
identical files on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def write(self, data: bytes, *, media_type: str) -> str: ...


def content_key(data: bytes, media_type: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    safe_type = media_type.strip().replace("/", "-").replace("+", "-") or "octet-stream"
    return f"{safe_type}/{digest[:2]}/{digest}"


class MemoryObjectStore:
    """Test double (and small-install stand-in)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def write(self, data: bytes, *, media_type: str) -> str:
        key = content_key(data, media_type)
        self.objects.setdefault(key, data)
        return key


class FileObjectStore:
    """Content-addressed files under one root (settings.object_store_root)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, data: bytes, *, media_type: str) -> str:
        key = content_key(data, media_type)
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return key
