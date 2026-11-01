from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from common.integrity import atomic_write_json, file_hash
from common.series_identity import SERIES_COMPOSER_CACHE_VERSION, composer_input_fingerprint


class ComposerOutputSignature(BaseModel):
    size: int
    mtime_ns: int
    digest: str | None = None


class ComposerCacheManifest(BaseModel):
    cache_version: str = SERIES_COMPOSER_CACHE_VERSION
    input_fingerprint: str
    outputs: dict[str, ComposerOutputSignature]


def _signature(path: Path) -> ComposerOutputSignature | None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    stat = resolved.stat()
    return ComposerOutputSignature(size=stat.st_size, mtime_ns=stat.st_mtime_ns, digest=file_hash(resolved))


def composer_cache_current(
    *,
    manifest_path: Path,
    input_fingerprint: str,
    outputs: list[Path],
    validate: Callable[[], bool],
) -> bool:
    if not manifest_path.is_file():
        return False
    try:
        manifest = ComposerCacheManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return False
    if manifest.cache_version != SERIES_COMPOSER_CACHE_VERSION or manifest.input_fingerprint != input_fingerprint:
        return False
    expected_paths = {str(path.expanduser().resolve()) for path in outputs}
    if set(manifest.outputs) != expected_paths:
        return False
    for output in outputs:
        resolved = output.expanduser().resolve()
        current = _signature(resolved)
        if current is None or current != manifest.outputs[str(resolved)]:
            return False
    return validate()


def commit_composer_cache(*, manifest_path: Path, input_fingerprint: str, outputs: list[Path]) -> None:
    signatures: dict[str, ComposerOutputSignature] = {}
    for output in outputs:
        resolved = output.expanduser().resolve()
        signature = _signature(resolved)
        if signature is None:
            raise FileNotFoundError(f"cannot cache missing composer output: {resolved}")
        signatures[str(resolved)] = signature
    manifest = ComposerCacheManifest(input_fingerprint=input_fingerprint, outputs=signatures)
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
