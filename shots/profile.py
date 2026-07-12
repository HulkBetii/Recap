from __future__ import annotations

from pathlib import Path

from common.schema import Shot, VideoProfile
from shots.cache import stable_hash

NO_PROFILE_HASH = "no-video-profile"


def video_profile_hash(path: Path | None) -> str:
    if path is None:
        return NO_PROFILE_HASH
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return NO_PROFILE_HASH
    return stable_hash(resolved.read_text(encoding="utf-8"))


def profile_cache_key(feature_key: str, profile_hash: str, end_credit_key: str = "end-credit-disabled") -> str:
    return stable_hash({
        "features": feature_key,
        "video_profile": profile_hash,
        "end_credit": end_credit_key,
    })


def non_story_reason(start: float, end: float, profile: VideoProfile | None) -> str | None:
    if profile is None:
        return None
    for item in profile.non_story_ranges:
        if start < item.end_s and end > item.start_s:
            return item.label
    return None


def apply_video_profile_to_shots(shots: list[Shot], profile: VideoProfile | None) -> tuple[list[Shot], int]:
    marked: list[Shot] = []
    n_non_story = 0
    for shot in shots:
        reason = non_story_reason(shot.tc_start, shot.tc_end, profile)
        if reason:
            n_non_story += 1
            marked.append(shot.model_copy(update={"is_story": False, "exclude_reason": reason, "is_usable": False}))
        else:
            marked.append(shot.model_copy(update={"is_story": True, "exclude_reason": None}))
    return marked, n_non_story
