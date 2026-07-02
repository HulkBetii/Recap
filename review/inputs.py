from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.schema import FilmMapSegment, validate_film_map


class ReviewInputError(RuntimeError):
    pass


def load_film_map(path: Path) -> list[FilmMapSegment]:
    if not path.is_file():
        raise ReviewInputError(f"film_map.json does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ReviewInputError("film_map.json must be a JSON array")
    segments = [FilmMapSegment.model_validate(item) for item in data]
    return validate_film_map(segments)


def load_duration(film_map_path: Path, film_map: list[FilmMapSegment]) -> tuple[float, list[str]]:
    warnings: list[str] = []
    meta_path = film_map_path.with_name(f"{film_map_path.stem}.meta.json")
    if meta_path.is_file():
        try:
            payload: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_duration = payload.get("duration_s", payload.get("duration"))
            if raw_duration is not None:
                duration = float(raw_duration)
                if payload.get("approximate_timecodes"):
                    warnings.append("film_map timecodes are approximate; avoid overly narrow source spans")
                for warning in payload.get("asr_warnings", []) or []:
                    warnings.append(f"ASR warning: {warning}")
                if duration > 0:
                    return duration, warnings
        except (json.JSONDecodeError, TypeError, ValueError):
            warnings.append(f"Could not parse meta duration from {meta_path}")
    else:
        warnings.append(f"Meta file not found: {meta_path}")
    if not film_map:
        raise ReviewInputError("film_map is empty and no duration metadata is available")
    warnings.append("Using max film_map tc_end as duration fallback")
    return max(segment.tc_end for segment in film_map), warnings
