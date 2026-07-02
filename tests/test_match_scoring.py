from __future__ import annotations

from common.schema import Shot
from match.scoring import ScoringWeights, score_shot


def make_shot(index, motion, face_count):  # type: ignore[no-untyped-def]
    return Shot(src="film.mp4", index=index, tc_start=index, tc_end=index+1, duration=1, thumb="x.jpg", motion_score=motion, face_count=face_count, face_area=0.1 if face_count else 0, brightness=0.5, is_usable=True)


def test_face_is_soft_bonus_not_hard_filter() -> None:
    weights = ScoringWeights(motion=0.6, face=0.18, bright=0.12, reuse=0.35)
    high_motion_no_face = make_shot(0, 0.95, 0)
    low_motion_face = make_shot(1, 0.1, 2)
    assert score_shot(high_motion_no_face, 0, weights) > score_shot(low_motion_face, 0, weights)


def test_reuse_penalty_lowers_score() -> None:
    weights = ScoringWeights(motion=0.6, face=0.18, bright=0.12, reuse=0.35)
    item = make_shot(0, 0.8, 0)
    assert score_shot(item, 2, weights) < score_shot(item, 0, weights)
