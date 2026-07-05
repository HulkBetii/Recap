from __future__ import annotations

from preflight.detect import FrameScore, decide_intro


def test_decide_intro_requires_confidence_threshold() -> None:
    scores = [FrameScore(time_s=0, story_score=0.2, non_story_score=0.8, label="title card")]
    intro, ranges, warnings = decide_intro(scores, confidence_threshold=0.75, uncertain_threshold=0.55, sample_every_s=5, max_intro_s=240)
    assert intro.detected is True
    assert ranges[0].end_s == 5
    assert warnings == []


def test_decide_intro_uncertain_does_not_exclude() -> None:
    scores = [FrameScore(time_s=0, story_score=0.3, non_story_score=0.6, label="opening credits")]
    intro, ranges, warnings = decide_intro(scores, confidence_threshold=0.75, uncertain_threshold=0.55, sample_every_s=5, max_intro_s=240)
    assert intro.detected is False
    assert ranges == []
    assert "uncertain_intro" in warnings

def test_decide_intro_detects_intercut_opening_sequence() -> None:
    scores = [
        FrameScore(time_s=0, story_score=0.1, non_story_score=0.8, label="opening credits"),
        FrameScore(time_s=5, story_score=0.9, non_story_score=0.1, label="story scene from the movie"),
        FrameScore(time_s=10, story_score=0.2, non_story_score=0.9, label="title card"),
        FrameScore(time_s=15, story_score=0.9, non_story_score=0.1, label="story scene from the movie"),
        FrameScore(time_s=20, story_score=0.1, non_story_score=0.95, label="opening credits"),
        FrameScore(time_s=25, story_score=0.9, non_story_score=0.1, label="story scene from the movie"),
        FrameScore(time_s=30, story_score=0.88, non_story_score=0.1, label="story scene from the movie"),
        FrameScore(time_s=35, story_score=0.91, non_story_score=0.1, label="story scene from the movie"),
    ]
    intro, ranges, warnings = decide_intro(scores, confidence_threshold=0.75, uncertain_threshold=0.55, sample_every_s=5, max_intro_s=240)
    assert intro.detected is True
    assert intro.end_s == 25
    assert ranges[0].end_s == 25
    assert "intercut_opening_sequence" in intro.reasons
    assert warnings == []
