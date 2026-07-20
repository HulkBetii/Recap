from __future__ import annotations

from datetime import datetime, timezone

from common.schema import IntroDetection, NonStoryRange, Shot, VideoProfile
from shots.profile import apply_video_profile_to_shots, profile_cache_key


def make_shot(index: int, start: float, end: float) -> Shot:
    return Shot(
        src="film.mp4",
        index=index,
        tc_start=start,
        tc_end=end,
        duration=round(end - start, 3),
        thumb=f"shots/film-{index:03d}.jpg",
        motion_score=0.5,
        face_count=0,
        face_area=0.0,
        brightness=0.5,
        is_usable=True,
    )


def test_apply_video_profile_marks_overlap_non_story() -> None:
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=20,
        intro=IntroDetection(detected=True, start_s=0, end_s=10, confidence=0.9, reasons=["opening credits"]),
        non_story_ranges=[NonStoryRange(start_s=0, end_s=10, label="intro_opening", confidence=0.9)],
        classifier="openclip",
        created_at=datetime.now(timezone.utc),
    )
    shots, n_non_story = apply_video_profile_to_shots([make_shot(0, 0, 5), make_shot(1, 12, 15)], profile)

    assert n_non_story == 1
    assert shots[0].is_story is False
    assert shots[0].is_usable is False
    assert shots[0].exclude_reason == "intro_opening"
    assert shots[1].is_story is True
    assert shots[1].exclude_reason is None


def test_apply_video_profile_marks_anime_opening_theme() -> None:
    profile = VideoProfile(
        input_path="anime.mp4",
        duration_s=200,
        intro=IntroDetection(detected=False, confidence=0, reasons=[]),
        non_story_ranges=[NonStoryRange(start_s=72, end_s=162, label="opening_theme", confidence=1.0)],
        classifier="heuristic",
        created_at=datetime.now(timezone.utc),
    )

    shots, n_non_story = apply_video_profile_to_shots([make_shot(0, 80, 85)], profile)

    assert n_non_story == 1
    assert shots[0].is_story is False
    assert shots[0].is_usable is False
    assert shots[0].exclude_reason == "opening_theme"

def test_apply_video_profile_without_profile_keeps_story() -> None:
    shots, n_non_story = apply_video_profile_to_shots([make_shot(0, 0, 5)], None)
    assert n_non_story == 0
    assert shots[0].is_story is True
    assert shots[0].is_usable is True


def test_profile_cache_key_changes_only_with_profile_hash() -> None:
    assert profile_cache_key("features-a", "profile-a") == profile_cache_key("features-a", "profile-a")
    assert profile_cache_key("features-a", "profile-a") != profile_cache_key("features-a", "profile-b")
    assert profile_cache_key("features-a", "profile-a", "credit-a") != profile_cache_key("features-a", "profile-a", "credit-b")
