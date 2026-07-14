from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from common.integrity import atomic_write_json, file_hash


class CommentaryCacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    cache_key: str
    raw_path: str
    audio_path: str
    raw_sha256: str
    audio_sha256: str
    duration_s: float
    lufs_i: float | None = None
    true_peak_dbfs: float | None = None
    asr_text_match: float | None = None
    requested_model: str | None = None
    actual_model: str | None = None


class CommentaryTtsCache:
    def __init__(self, work_dir: Path, *, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.manifest_path = work_dir / "manifest.json"
        self.raw_dir = work_dir / "raw"
        self.audio_dir = work_dir / "audio"

    def prepare(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        if self.force and self.manifest_path.exists():
            self.manifest_path.unlink()

    def load(self) -> dict[str, CommentaryCacheEntry]:
        if not self.manifest_path.is_file():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return {key: CommentaryCacheEntry.model_validate(value) for key, value in payload.items()}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def save(self, entries: dict[str, CommentaryCacheEntry]) -> None:
        atomic_write_json(
            self.manifest_path,
            {key: value.model_dump(mode="json") for key, value in sorted(entries.items())},
        )

    def raw_path(self, cache_key: str) -> Path:
        return self.raw_dir / f"{cache_key}.mp3"

    def audio_path(self, cache_key: str) -> Path:
        return self.audio_dir / f"{cache_key}.mp3"

    def get_valid(
        self,
        entries: dict[str, CommentaryCacheEntry],
        cache_key: str,
    ) -> CommentaryCacheEntry | None:
        entry = entries.get(cache_key)
        if entry is None:
            return None
        raw_path = self.work_dir / entry.raw_path
        audio_path = self.work_dir / entry.audio_path
        if file_hash(raw_path) != entry.raw_sha256 or file_hash(audio_path) != entry.audio_sha256:
            entries.pop(cache_key, None)
            return None
        return entry


def cache_payload(**values: Any) -> dict[str, Any]:
    return dict(sorted(values.items()))
