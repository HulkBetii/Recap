from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from common.integrity import atomic_write_json, file_hash, media_identity_hash


SERIES_STAGE_CACHE_VERSION = "series-recap-stages-v1"


class OutputSignature(BaseModel):
    size: int
    mtime_ns: int
    digest: str | None = None


class StageCacheEntry(BaseModel):
    input_fingerprint: str
    outputs: dict[str, OutputSignature]


class SeriesStageManifest(BaseModel):
    cache_version: str = SERIES_STAGE_CACHE_VERSION
    stages: dict[str, StageCacheEntry] = Field(default_factory=dict)


def output_signature(path: Path) -> OutputSignature | None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    stat = resolved.stat()
    digest = (
        media_identity_hash(resolved)
        if resolved.suffix.lower() in {".mp3", ".mp4", ".m4a", ".wav"}
        else file_hash(resolved)
    )
    return OutputSignature(size=stat.st_size, mtime_ns=stat.st_mtime_ns, digest=digest)


class SeriesStageCache:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.expanduser().resolve()
        self.manifest = self._load()

    def _load(self) -> SeriesStageManifest:
        if not self.manifest_path.is_file():
            return SeriesStageManifest()
        try:
            manifest = SeriesStageManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError, json.JSONDecodeError):
            return SeriesStageManifest()
        if manifest.cache_version != SERIES_STAGE_CACHE_VERSION:
            return SeriesStageManifest()
        return manifest

    def is_current(
        self,
        *,
        stage: str,
        input_fingerprint: str,
        outputs: list[Path],
        validate: Callable[[], bool],
    ) -> bool:
        entry = self.manifest.stages.get(stage)
        if entry is None or entry.input_fingerprint != input_fingerprint:
            return False
        expected_paths = {str(path.expanduser().resolve()) for path in outputs}
        if set(entry.outputs) != expected_paths:
            return False
        for path in outputs:
            resolved = path.expanduser().resolve()
            current = output_signature(resolved)
            if current is None or current != entry.outputs[str(resolved)]:
                return False
        return validate()

    def commit(self, *, stage: str, input_fingerprint: str, outputs: list[Path]) -> None:
        signatures: dict[str, OutputSignature] = {}
        for path in outputs:
            resolved = path.expanduser().resolve()
            signature = output_signature(resolved)
            if signature is None:
                raise FileNotFoundError(f"cannot cache missing stage output: {resolved}")
            signatures[str(resolved)] = signature
        self.manifest.stages[stage] = StageCacheEntry(
            input_fingerprint=input_fingerprint,
            outputs=signatures,
        )
        atomic_write_json(self.manifest_path, self.manifest.model_dump(mode="json"))
