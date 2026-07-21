from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from common.integrity import file_hash, stable_hash

REVIEW_CACHE_VERSION = "review-v1"
REVIEW_PROMPT_VERSION = "review-prompts-v2"

REVIEW_CONFIG_FIELDS = (
    "target_ratio",
    "tts_cps",
    "min_coverage",
    "max_qa_iterations",
    "max_qa_rewrites_per_iteration",
    "content_type",
    "hook_mode",
    "opening_coherence_qa",
    "micro_beats",
    "target_beat_audio_s",
    "max_beat_audio_s",
    "style_preset",
    "style_strength",
    "style_qa",
    "target_sentence_chars",
    "max_sentence_chars",
    "drop_non_story_beats",
    "non_story_tail_s",
    "llm_backend",
)


@dataclass(frozen=True)
class ReviewIdentity:
    film_map_hash: str
    film_map_meta_hash: str | None
    story_map_hash: str | None
    video_profile_hash: str | None
    context_file_hash: str | None
    config_hash: str
    core_input_hash: str
    cache_key: str


def _value(settings: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def build_review_identity(
    *,
    film_map_path: Path,
    settings: Mapping[str, Any] | object,
    style_sample_path: Path | None,
    story_map_path: Path | None,
    video_profile_path: Path | None,
    context_file_path: Path | None = None,
) -> ReviewIdentity:
    film_map_path = film_map_path.expanduser().resolve()
    for label, path in (
        ("style sample", style_sample_path),
        ("story map", story_map_path),
        ("video profile", video_profile_path),
        ("context file", context_file_path),
    ):
        if path is not None and not path.expanduser().resolve().is_file():
            raise FileNotFoundError(f"{label} does not exist: {path.expanduser().resolve()}")
    film_map_meta_path = film_map_path.with_name(f"{film_map_path.stem}.meta.json")
    film_map_digest = file_hash(film_map_path)
    if film_map_digest is None:
        raise FileNotFoundError(f"film map does not exist: {film_map_path}")
    film_map_meta_digest = file_hash(film_map_meta_path)
    story_map_digest = file_hash(story_map_path) if story_map_path else None
    video_profile_digest = file_hash(video_profile_path) if video_profile_path else None
    context_file_digest = file_hash(context_file_path) if context_file_path else None
    config_payload = {name: _value(settings, name) for name in REVIEW_CONFIG_FIELDS}
    config_payload["target_ratio"] = str(config_payload.get("target_ratio"))
    for name in ("tts_cps", "min_coverage", "target_beat_audio_s", "max_beat_audio_s", "non_story_tail_s"):
        if config_payload.get(name) is not None:
            config_payload[name] = float(config_payload[name])
    for name in ("max_qa_iterations", "max_qa_rewrites_per_iteration", "target_sentence_chars", "max_sentence_chars"):
        if config_payload.get(name) is not None:
            config_payload[name] = int(config_payload[name])
    config_payload.update(
        {
            "style_sample_hash": file_hash(style_sample_path) if style_sample_path else None,
            "context_file_hash": context_file_digest,
            "prompt_version": REVIEW_PROMPT_VERSION,
        }
    )
    config_digest = stable_hash(config_payload)
    core_input_hash = stable_hash(
        {
            "film_map_hash": film_map_digest,
            "film_map_meta_hash": film_map_meta_digest,
            "story_map_hash": story_map_digest,
            "video_profile_hash": video_profile_digest,
            "context_file_hash": context_file_digest,
        }
    )
    return ReviewIdentity(
        film_map_hash=film_map_digest,
        film_map_meta_hash=film_map_meta_digest,
        story_map_hash=story_map_digest,
        video_profile_hash=video_profile_digest,
        context_file_hash=context_file_digest,
        config_hash=config_digest,
        core_input_hash=core_input_hash,
        cache_key=stable_hash({"core_input_hash": core_input_hash, "config_hash": config_digest, "cache_version": REVIEW_CACHE_VERSION}),
    )
