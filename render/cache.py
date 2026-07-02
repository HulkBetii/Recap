from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def stable_hash(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


class RenderCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.temp_dir = self.work_dir / "temp_clips"
        self.cache_hits: list[str] = []

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def temp_path(self, cache_key: str) -> Path:
        return self.temp_dir / f"{cache_key}.mp4"

    def get_cached_temp(self, cache_key: str) -> Path | None:
        path = self.temp_path(cache_key)
        if path.is_file():
            self.cache_hits.append(path.relative_to(self.work_dir).as_posix())
            return path
        return None
