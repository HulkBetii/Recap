from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np

from common.integrity import file_hash, media_identity_hash, stable_hash
from common.schema import Shot, ShotVisualIndexFile, validate_shot_visual_index

PREPROCESSING_VERSION = "siglip2-fixed64-v1"



def visual_index_config_hash(
    *,
    film_path: Path,
    shots_path: Path,
    embedding_mode: str,
    embedding_model: str,
    keyframes_per_shot: int,
    frame_sampling: str,
) -> str:
    return stable_hash(
        {
            "film_hash": media_identity_hash(film_path),
            "shots_hash": file_hash(shots_path),
            "embedding_mode": embedding_mode,
            "embedding_model": embedding_model,
            "keyframes_per_shot": keyframes_per_shot,
            "frame_sampling": frame_sampling,
            "preprocessing_version": PREPROCESSING_VERSION,
        }
    )


def resolve_ref(index_path: Path, ref: str) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else index_path.parent / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vector_is_valid(path: Path, *, embedding_dim: int, expected_sha256: str | None = None) -> bool:
    if not path.is_file():
        return False
    try:
        vector = np.load(path, allow_pickle=False)
    except (OSError, ValueError):
        return False
    if vector.ndim != 1 or vector.shape[0] != embedding_dim:
        return False
    values = vector.astype("float32", copy=False)
    if not np.isfinite(values).all() or float(np.linalg.norm(values)) <= 0:
        return False
    return expected_sha256 is None or sha256_file(path) == expected_sha256


def validate_visual_index_artifacts(
    index_path: Path,
    index: ShotVisualIndexFile,
    shots: list[Shot] | None = None,
    *,
    require_frames: bool = False,
    require_calibration: bool = False,
) -> ShotVisualIndexFile:
    validated = validate_shot_visual_index(index, shots)
    if validated.meta.embedding_dim <= 0:
        raise ValueError("visual index embedding_dim must be > 0")
    if require_calibration and validated.meta.version != "1.1":
        raise ValueError("visual index is legacy/uncalibrated and must be rebuilt")
    if require_calibration and (validated.meta.logit_scale is None or validated.meta.logit_bias is None):
        raise ValueError("visual index is legacy/uncalibrated and must be rebuilt")
    require_checksums = validated.meta.version == "1.1"
    for item in validated.shots:
        if len(item.keyframes) != validated.meta.keyframes_per_shot:
            raise ValueError(f"visual index keyframe count is invalid for shot #{item.shot_index}")
        if require_checksums and not item.shot_embedding_sha256:
            raise ValueError(f"visual index pooled embedding checksum is missing for shot #{item.shot_index}")
        pooled_path = resolve_ref(index_path, item.shot_embedding_ref)
        if not vector_is_valid(
            pooled_path,
            embedding_dim=validated.meta.embedding_dim,
            expected_sha256=item.shot_embedding_sha256,
        ):
            raise ValueError(f"visual index pooled embedding is invalid for shot #{item.shot_index}")
        for frame in item.keyframes:
            if require_checksums and not frame.embedding_sha256:
                raise ValueError(f"visual index keyframe embedding checksum is missing for shot #{item.shot_index}")
            if require_frames and not resolve_ref(index_path, frame.frame_path).is_file():
                raise ValueError(f"visual index frame is missing for shot #{item.shot_index}")
            vector_path = resolve_ref(index_path, frame.embedding_ref)
            if not vector_is_valid(
                vector_path,
                embedding_dim=validated.meta.embedding_dim,
                expected_sha256=frame.embedding_sha256,
            ):
                raise ValueError(f"visual index keyframe embedding is invalid for shot #{item.shot_index}")
    return validated


def metadata_is_current(
    index: ShotVisualIndexFile,
    *,
    film_path: Path,
    shots_path: Path,
    config_hash: str,
) -> bool:
    meta = index.meta
    return (
        meta.version == "1.1"
        and meta.film_hash == media_identity_hash(film_path)
        and meta.shots_hash == file_hash(shots_path)
        and meta.config_hash == config_hash
        and meta.preprocessing_version == PREPROCESSING_VERSION
        and meta.logit_scale is not None
        and math.isfinite(meta.logit_scale)
        and meta.logit_scale > 0
        and meta.logit_bias is not None
        and math.isfinite(meta.logit_bias)
    )


def visual_index_artifact_hash(index_path: Path) -> str:
    index_hash = file_hash(index_path)
    try:
        index = ShotVisualIndexFile.model_validate_json(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return stable_hash({"index": index_hash, "sidecars": "unreadable"})
    refs = sorted(
        {item.shot_embedding_ref for item in index.shots}
        | {frame.embedding_ref for item in index.shots for frame in item.keyframes}
    )
    sidecars = []
    for ref in refs:
        path = resolve_ref(index_path, ref)
        try:
            checksum = sha256_file(path) if path.is_file() else None
        except OSError:
            checksum = None
        sidecars.append((ref, checksum))
    return stable_hash({"index": index_hash, "sidecars": sidecars})
