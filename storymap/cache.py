from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


class StoryMapCache:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.work_dir / name

    def read_json(self, name: str) -> Any | None:
        path = self.path(name)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, name: str, data: Any) -> None:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
