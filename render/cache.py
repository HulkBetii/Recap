from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable


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
        self.audio_dir = self.work_dir / "audio_cache"
        self.cache_hits: list[str] = []

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def temp_path(self, cache_key: str) -> Path:
        return self.temp_dir / f"{cache_key}.mp4"

    def get_cached_temp(self, cache_key: str, validator: Callable[[Path], bool] | None = None) -> Path | None:
        path = self.temp_path(cache_key)
        if path.is_file() and (validator is None or validator(path)):
            self.cache_hits.append(path.relative_to(self.work_dir).as_posix())
            return path
        return None

    def audio_path(self, kind: str, cache_key: str) -> Path:
        return self.audio_dir / f"{kind}-{cache_key}.m4a"

    def get_cached_audio(self, kind: str, cache_key: str) -> Path | None:
        path = self.audio_path(kind, cache_key)
        if path.is_file() and path.stat().st_size > 0:
            self.cache_hits.append(path.relative_to(self.work_dir).as_posix())
            return path
        return None
