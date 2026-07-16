from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from common.integrity import atomic_write_json
from review.integrity import REVIEW_CACHE_VERSION

GENERATED_FILES = (
    "outline.json",
    "narration.json",
    "narration_consistent.json",
    "narration_style_checked.json",
    "qa.json",
    "repetition_qa.json",
    "style_qa.json",
    "style_config.json",
    "opening_coherence.json",
    "opening_coherence_revision.json",
    "pre_story_beats.json",
    "micro_beats.json",
    "non_story_beats.json",
)
GENERATED_DIRS = ("revisions", "style_revisions")


class ReviewCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self) -> None:
        if self.force:
            self.invalidate_generated()
            manifest = self.work_dir / "cache_manifest.json"
            if manifest.exists():
                manifest.unlink()
        (self.work_dir / "revisions").mkdir(parents=True, exist_ok=True)
        (self.work_dir / "style_revisions").mkdir(parents=True, exist_ok=True)

    def reconcile(self, cache_key: str) -> bool:
        manifest_path = self.work_dir / "cache_manifest.json"
        previous_key = None
        if manifest_path.is_file():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                if payload.get("cache_version") == REVIEW_CACHE_VERSION:
                    previous_key = payload.get("cache_key")
            except (OSError, json.JSONDecodeError):
                previous_key = None
        current = previous_key == cache_key
        if current:
            current = self._generated_json_is_readable()
        if not current:
            self.invalidate_generated()
        atomic_write_json(manifest_path, {"cache_version": REVIEW_CACHE_VERSION, "cache_key": cache_key})
        return current

    def _generated_json_is_readable(self) -> bool:
        paths = [self.work_dir / name for name in GENERATED_FILES]
        for dirname in GENERATED_DIRS:
            target_dir = self.work_dir / dirname
            if target_dir.is_dir():
                paths.extend(target_dir.glob("*.json"))
        try:
            for path in paths:
                if path.is_file() and path.suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return True

    def invalidate_generated(self) -> None:
        for name in GENERATED_FILES:
            target = self.work_dir / name
            if target.exists():
                target.unlink()
        for dirname in GENERATED_DIRS:
            target_dir = self.work_dir / dirname
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

    def path(self, relative_path: str) -> Path:
        return self.work_dir / relative_path

    def has(self, relative_path: str) -> bool:
        exists = self.path(relative_path).exists()
        if exists:
            self.cache_hits.append(relative_path)
        return exists

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.path(relative_path).read_text(encoding="utf-8"))

    def write_json(self, relative_path: str, data: object) -> None:
        path = self.path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, BaseModel):
            text = data.model_dump_json(by_alias=True, indent=2)
        elif isinstance(data, list) and all(isinstance(item, BaseModel) for item in data):
            text = "[\n" + ",\n".join(item.model_dump_json(by_alias=True, indent=2) for item in data) + "\n]"
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
