from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from common.integrity import atomic_write_json
from common.schema import TranscriptQuality, TranscriptSegment, TranslatedSegment, VisionSegment
from ingest.integrity import INGEST_CACHE_VERSION

STAGE_ORDER = ("audio", "transcript", "correction", "translation", "vision")
STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "audio": ("audio.wav",),
    "transcript": ("transcript_raw.json", "transcript_text.json", "transcript_aligned.json", "transcript_quality.json"),
    "correction": ("transcript_corrected.json", "transcript_correction.meta.json"),
    "translation": ("translated.json",),
    "vision": ("vision.json",),
}
STAGE_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "audio": (),
    "transcript": ("openai_chunks", "local_asr_chunks"),
    "correction": (),
    "translation": (),
    "vision": ("frames",),
}


class StageCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.work_dir / "cache_manifest.json"
        self.manifest: dict[str, Any] = {"cache_version": INGEST_CACHE_VERSION, "keys": {}}

    def path(self, relative_path: str) -> Path:
        return self.work_dir / relative_path

    def prepare(self) -> None:
        if self.force:
            self.invalidate_from("audio")
            if self.manifest_path.exists():
                self.manifest_path.unlink()
        elif self.manifest_path.is_file():
            try:
                payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if payload.get("cache_version") == INGEST_CACHE_VERSION and isinstance(payload.get("keys"), dict):
                    self.manifest = payload
                else:
                    self.invalidate_from("audio")
            except (OSError, json.JSONDecodeError):
                self.invalidate_from("audio")
        elif any((self.work_dir / name).exists() for names in STAGE_ARTIFACTS.values() for name in names):
            self.invalidate_from("audio")
        (self.work_dir / "frames").mkdir(parents=True, exist_ok=True)

    def invalidate_from(self, stage: str) -> None:
        start = STAGE_ORDER.index(stage)
        for current in STAGE_ORDER[start:]:
            for name in STAGE_ARTIFACTS[current]:
                target = self.work_dir / name
                if target.exists():
                    target.unlink()
            for name in STAGE_DIRECTORIES[current]:
                target = self.work_dir / name
                if target.exists():
                    shutil.rmtree(target)
            self.manifest.setdefault("keys", {}).pop(current, None)

    def stage_current(self, stage: str, key: str, required: tuple[str, ...]) -> bool:
        current = self.manifest.get("keys", {}).get(stage) == key
        if current:
            current = all(self._artifact_valid(name) for name in required)
        if current:
            current = self._stage_payload_valid(stage)
        if not current:
            self.invalidate_from(stage)
        return current

    def commit_stage(self, stage: str, key: str) -> None:
        self.manifest.setdefault("keys", {})[stage] = key
        atomic_write_json(self.manifest_path, self.manifest)

    def _artifact_valid(self, relative_path: str) -> bool:
        path = self.path(relative_path)
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
        return True

    def _stage_payload_valid(self, stage: str) -> bool:
        try:
            if stage == "transcript":
                [TranscriptSegment.model_validate(item) for item in self.read_json("transcript_aligned.json")]
                TranscriptQuality.model_validate(self.read_json("transcript_quality.json"))
            elif stage == "correction" and self.path("transcript_corrected.json").is_file():
                [TranscriptSegment.model_validate(item) for item in self.read_json("transcript_corrected.json")]
                if not isinstance(self.read_json("transcript_correction.meta.json"), dict):
                    return False
            elif stage == "translation":
                [TranslatedSegment.model_validate(item) for item in self.read_json("translated.json")]
            elif stage == "vision":
                [VisionSegment.model_validate(item) for item in self.read_json("vision.json")]
        except (OSError, ValueError, TypeError, KeyError):
            return False
        return True

    def has(self, relative_path: str) -> bool:
        path = self.path(relative_path)
        exists = path.exists()
        if exists:
            self.cache_hits.append(relative_path)
        return exists

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.path(relative_path).read_text(encoding="utf-8"))

    def write_json(self, relative_path: str, data: object) -> None:
        path = self.path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        elif isinstance(data, list) and all(isinstance(item, BaseModel) for item in data):
            text = "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in data) + "\n]"
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
