from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from common.schema import AnimeContext, AnimeNonStoryRange, EpisodeMemory, NonStoryRange, SeriesManifest, Shot, validate_shots

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a runtime dependency
    yaml = None  # type: ignore[assignment]


def load_shots(path: Path) -> list[Shot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = [Shot.model_validate(item) for item in data]
    return validate_shots(shots)

def load_structured_file(path: Path) -> object:
    resolved = path.expanduser().resolve()
    suffix = resolved.suffix.lower()
    text = resolved.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML files")
        return yaml.safe_load(text) or {}
    raise ValueError("input file must be .json, .yaml or .yml")

@dataclass(frozen=True)
class ReviewContextBundle:
    anime_context: AnimeContext | None
    episode_memory: EpisodeMemory | None

def load_anime_context(path: Path) -> AnimeContext:
    data = load_structured_file(path)
    if isinstance(data, dict) and (data.get("kind") == "episode_memory" or "current" in data):
        memory = EpisodeMemory.model_validate(data)
        if memory.anime_context is None:
            raise ValueError("episode memory context does not contain anime_context")
        return memory.anime_context
    return AnimeContext.model_validate(data)

def load_review_context(path: Path) -> ReviewContextBundle:
    data = load_structured_file(path)
    if isinstance(data, dict) and (data.get("kind") == "episode_memory" or "current" in data):
        memory = EpisodeMemory.model_validate(data)
        return ReviewContextBundle(anime_context=memory.anime_context, episode_memory=memory)
    return ReviewContextBundle(anime_context=AnimeContext.model_validate(data), episode_memory=None)

def load_series_manifest(path: Path) -> SeriesManifest:
    data = load_structured_file(path)
    return SeriesManifest.model_validate(data)

def load_manual_non_story_ranges(path: Path) -> list[NonStoryRange]:
    data = load_structured_file(path)
    if isinstance(data, dict):
        raw_ranges = data.get("non_story_ranges", [])
    else:
        raw_ranges = data
    if not isinstance(raw_ranges, list):
        raise ValueError("manual non-story ranges must be a list or an object with non_story_ranges")
    ranges = [AnimeNonStoryRange.model_validate(item) for item in raw_ranges]
    return [NonStoryRange.model_validate(item.model_dump(mode="json")) for item in ranges]
