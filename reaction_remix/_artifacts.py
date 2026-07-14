from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from common.integrity import atomic_write_json, file_hash
from common.schema import ReactionStageMeta

ModelT = TypeVar("ModelT", bound=BaseModel)


def load_current_artifact(
    output_path: Path,
    meta_path: Path,
    model_type: type[ModelT],
    *,
    stage: str,
    algorithm_version: str,
    input_hashes: dict[str, str],
    config_hash: str,
) -> ModelT | None:
    if not output_path.is_file() or not meta_path.is_file():
        return None
    try:
        model = model_type.model_validate_json(output_path.read_text(encoding="utf-8"))
        meta = ReactionStageMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        meta.stage != stage
        or meta.algorithm_version != algorithm_version
        or meta.input_hashes != input_hashes
        or meta.config_hash != config_hash
        or meta.output_hashes.get(output_path.name) != file_hash(output_path)
    ):
        return None
    return model


def write_artifact(
    output_path: Path,
    meta_path: Path,
    model: BaseModel,
    *,
    stage: str,
    algorithm_version: str,
    input_hashes: dict[str, str],
    config_hash: str,
    cache_hits: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ReactionStageMeta:
    atomic_write_json(output_path, model.model_dump(mode="json"))
    digest = file_hash(output_path)
    if digest is None:
        raise OSError(f"could not hash written artifact: {output_path}")
    meta = ReactionStageMeta(
        stage=stage,
        algorithm_version=algorithm_version,
        input_hashes=input_hashes,
        config_hash=config_hash,
        output_hashes={output_path.name: digest},
        cache_hits=cache_hits or [],
        created_at=datetime.now(timezone.utc),
        warnings=warnings or [],
    )
    atomic_write_json(meta_path, meta.model_dump(mode="json"))
    return meta
