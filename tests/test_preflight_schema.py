from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.schema import IntroDetection, NonStoryRange, VideoProfile


def test_video_profile_accepts_detected_intro_range() -> None:
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=100,
        intro=IntroDetection(detected=True, start_s=0, end_s=12, confidence=0.8, reasons=["title card"]),
        non_story_ranges=[NonStoryRange(start_s=0, end_s=12, label="intro_opening", confidence=0.8)],
        classifier="heuristic",
        created_at=datetime.now(timezone.utc),
    )
    assert profile.non_story_ranges[0].label == "intro_opening"


def test_video_profile_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError):
        NonStoryRange(start_s=10, end_s=10, label="intro_opening", confidence=0.8)
    with pytest.raises(ValueError):
        IntroDetection(detected=True, start_s=10, end_s=5, confidence=0.8)
