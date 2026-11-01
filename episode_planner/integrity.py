from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from common.integrity import file_hash, media_identity_hash, stable_hash

EPISODE_PLANNER_CACHE_VERSION = "episode-planner-v1"

EPISODE_PLANNER_CONFIG_FIELDS = (
    "episode_key",
    "episode_number",
    "recap_mode",
    "recap_full_threshold",
    "recap_quick_threshold",
    "recap_merge_threshold",
    "quick_target_ratio",
    "quick_min_coverage",
)

def _value(settings: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)

def episode_planner_config_hash(settings: Mapping[str, Any] | object) -> str:
    payload = {name: _value(settings, name) for name in EPISODE_PLANNER_CONFIG_FIELDS}
    for name in (
        "recap_full_threshold",
        "recap_quick_threshold",
        "recap_merge_threshold",
        "quick_target_ratio",
        "quick_min_coverage",
    ):
        if payload.get(name) is not None:
            payload[name] = float(payload[name])
    if payload.get("episode_number") is not None:
        payload["episode_number"] = str(payload["episode_number"])
    return stable_hash({"fields": payload, "cache_version": EPISODE_PLANNER_CACHE_VERSION})

def episode_planner_input_hashes(
    *,
    film: Path,
    film_map_path: Path,
    story_map_path: Path | None,
    video_profile_path: Path | None,
    anime_context_path: Path | None,
    series_manifest_path: Path | None,
) -> dict[str, str | None]:
    return {
        "source_hash": media_identity_hash(film),
        "film_map_hash": file_hash(film_map_path),
        "story_map_hash": file_hash(story_map_path),
        "video_profile_hash": file_hash(video_profile_path),
        "anime_context_hash": file_hash(anime_context_path),
        "series_manifest_hash": file_hash(series_manifest_path),
    }
