from __future__ import annotations

import numpy as np

from common.schema import Shot
from shots.detect import ShotSpan
from shots.end_credits import (
    apply_end_credit_marking,
    credit_like_score,
    is_credit_only_frame,
    tail_shot_spans,
)


def black_frame() -> np.ndarray:
    return np.zeros((180, 320, 3), dtype=np.uint8)


def test_credit_classifier_accepts_blank_and_sparse_credit_frames() -> None:
    sparse_credit = black_frame()
    for row in range(30, 150, 18):
        sparse_credit[row : row + 3, 130:190] = 220

    assert is_credit_only_frame(black_frame()) is True
    assert is_credit_only_frame(sparse_credit) is True
    assert credit_like_score([black_frame(), sparse_credit, black_frame()]) == 1.0


def test_credit_classifier_preserves_story_and_composite_credit_frames() -> None:
    story = black_frame()
    story[25:155, 20:210] = (30, 120, 210)
    composite = story.copy()
    for row in range(35, 150, 14):
        composite[row : row + 2, 245:300] = 220

    assert is_credit_only_frame(story) is False
    assert is_credit_only_frame(composite) is False
    assert credit_like_score([story, composite]) == 0.0


def test_tail_spans_and_marking_are_backward_compatible() -> None:
    spans = [ShotSpan(index=0, tc_start=0, tc_end=5), ShotSpan(index=1, tc_start=500, tc_end=505)]
    assert [span.index for span in tail_shot_spans(spans, duration_s=600, tail_s=120)] == [1]

    shot = Shot(
        src="film.mp4",
        index=0,
        tc_start=0,
        tc_end=5,
        duration=5,
        thumb="0.jpg",
        motion_score=0.1,
        face_count=0,
        face_area=0,
        brightness=0.1,
        is_usable=True,
    )
    assert shot.is_end_credit is False
    assert shot.credit_like_score == 0.0
    marked = apply_end_credit_marking([shot], {0: 0.6}, threshold=0.6)[0]
    assert marked.is_end_credit is True
    assert marked.credit_like_score == 0.6
