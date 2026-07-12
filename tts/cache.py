from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from common.schema import TtsManifestEntry


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_cache_key(
    *,
    provider: str,
    voice_id: str,
    model: str,
    speed: float,
    narration: str,
    normalized: bool,
    provider_config: dict[str, object] | None = None,
) -> str:
    return stable_hash(json.dumps({
        "provider": provider,
        "voice_id": voice_id,
        "model": model,
        "speed": speed,
        "narration": narration,
        "normalized": normalized,
        "provider_config": provider_config,
    }, ensure_ascii=False, sort_keys=True))


class TtsCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.audio_dir = self.work_dir / "audio"
        self.raw_dir = self.work_dir / "raw"
        self.manifest_path = self.work_dir / "manifest.json"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            for path in (self.audio_dir, self.raw_dir):
                if path.exists():
                    shutil.rmtree(path)
            if self.manifest_path.exists():
                self.manifest_path.unlink()
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> dict[str, TtsManifestEntry]:
        if not self.manifest_path.exists():
            return {}
        data: dict[str, Any] = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {key: TtsManifestEntry.model_validate(value) for key, value in data.items()}

    def save_manifest(self, manifest: dict[str, TtsManifestEntry]) -> None:
        payload = {key: value.model_dump() for key, value in manifest.items()}
        self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get_cached(self, manifest: dict[str, TtsManifestEntry], beat_id: int, cache_key: str) -> Path | None:
        entry = manifest.get(str(beat_id))
        if not entry or entry.cache_key != cache_key:
            return None
        audio_path = self.work_dir / entry.audio_path
        if not audio_path.is_file():
            return None
        self.cache_hits.append(f"audio/{beat_id}.mp3")
        return audio_path

    def audio_path(self, beat_id: int) -> Path:
        return self.audio_dir / f"{beat_id}.mp3"

    def raw_path(self, beat_id: int) -> Path:
        return self.raw_dir / f"{beat_id}.mp3"
