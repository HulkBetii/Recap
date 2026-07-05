from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def stable_hash(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class ShotsCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            for name in ("detection.json", "features.json", "profile_marking.json"):
                path = self.work_dir / name
                if path.exists():
                    path.unlink()
            thumbs = self.work_dir / "thumbs"
            if thumbs.exists():
                shutil.rmtree(thumbs)
        (self.work_dir / "thumbs").mkdir(parents=True, exist_ok=True)

    def path(self, relative_path: str) -> Path:
        return self.work_dir / relative_path

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.path(relative_path).read_text(encoding="utf-8"))

    def write_json(self, relative_path: str, data: object) -> None:
        path = self.path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def read_cached(self, relative_path: str, cache_key: str) -> Any | None:
        path = self.path(relative_path)
        if not path.exists():
            return None
        payload = self.read_json(relative_path)
        if payload.get("cache_key") != cache_key:
            return None
        self.cache_hits.append(relative_path)
        return payload.get("data")

    def write_cached(self, relative_path: str, cache_key: str, data: object) -> None:
        self.write_json(relative_path, {"cache_key": cache_key, "data": data})
