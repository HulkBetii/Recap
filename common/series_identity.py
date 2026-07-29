from __future__ import annotations

from pathlib import Path

from common.integrity import file_hash, stable_hash


SERIES_COMPOSER_CACHE_VERSION = "series-composer-v1"
COMPOSER_ARTIFACT_NAMES = (
    "episode_meta.json",
    "episode_memory.json",
    "film_map.json",
    "film_map.meta.json",
    "story_map.json",
    "video_profile.json",
)


def composer_input_fingerprint(
    *,
    manifest_path: Path,
    episode_run_dirs: dict[str, Path],
    settings: dict[str, object],
) -> str:
    episodes = [
        {
            "episode_key": episode_key,
            "artifacts": {name: file_hash(run_dir / name) for name in COMPOSER_ARTIFACT_NAMES},
        }
        for episode_key, run_dir in episode_run_dirs.items()
    ]
    return stable_hash(
        {
            "cache_version": SERIES_COMPOSER_CACHE_VERSION,
            "manifest_hash": file_hash(manifest_path),
            "episodes": episodes,
            "settings": settings,
        }
    )
