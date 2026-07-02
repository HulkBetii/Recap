from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_hash(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class MatchCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.plan_path = self.work_dir / "plan.json"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self) -> None:
        if self.force and self.plan_path.exists():
            self.plan_path.unlink()

    def read_plan(self, cache_key: str) -> dict[str, Any] | None:
        if not self.plan_path.exists():
            return None
        payload = json.loads(self.plan_path.read_text(encoding="utf-8"))
        if payload.get("cache_key") != cache_key:
            return None
        self.cache_hits.append("plan.json")
        return payload

    def write_plan(self, cache_key: str, edl: list[dict], meta: dict) -> None:
        self.plan_path.write_text(
            json.dumps({"cache_key": cache_key, "edl": edl, "meta": meta}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
