from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def stable_hash(data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_identity_hash(path: Path) -> str:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    return stable_hash({"path": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
