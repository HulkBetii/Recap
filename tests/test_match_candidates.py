from __future__ import annotations

from common.schema import Shot
from match.candidates import candidates_for_window, effective_candidate_capacity, plan_candidates, widen_until_enough


def shot(index, start, end, usable=True):  # type: ignore[no-untyped-def]
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=usable)


def test_candidates_intersect_window_and_filter_usable() -> None:
    shots = [shot(0, 0, 2), shot(1, 3, 4, usable=False), shot(2, 5, 6)]
    assert [s.index for s in candidates_for_window(shots, 1, 5)] == [0]


def test_widen_until_enough_expands_window() -> None:
    shots = [shot(0, 0, 2), shot(1, 10, 14)]
    start, end, candidates, count = widen_until_enough(shots=shots, start=10, end=11, needed_duration=3, margin=2, max_widen=2)
    assert count > 0
    assert end > 11
    assert candidates


def test_effective_capacity_caps_each_shot_at_max_clip() -> None:
    shots = [shot(0, 0, 8)]
    assert effective_candidate_capacity(shots, 0, 8, max_clip=5, min_visual_clip=0.6) == 5


def test_dark_local_candidates_prevent_widen() -> None:
    dark = shot(0, 10, 18, usable=False).model_copy(
        update={"brightness": 0.05, "unusable_reasons": ["too_dark"]}
    )
    plan = plan_candidates(
        shots=[dark],
        start=10,
        end=18,
        needed_duration=4,
        margin=15,
        max_widen=3,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )
    assert plan.widen_count == 0
    assert plan.dark_candidate_ids == {0}
    assert plan.total_capacity_s == 5


def test_candidate_plan_never_returns_window_beyond_max_widen() -> None:
    plan = plan_candidates(
        shots=[],
        start=100,
        end=110,
        needed_duration=5,
        margin=10,
        max_widen=2,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )
    assert plan.window_start == 80
    assert plan.window_end == 130
    assert plan.widen_count == 2
    assert plan.capacity_exhausted is True


def test_dark_fallback_rejects_invalid_and_non_story_shots() -> None:
    invalid = [
        shot(0, 0, 5, usable=False).model_copy(update={"brightness": 0.0, "unusable_reasons": ["no_frames"]}),
        shot(1, 5, 10, usable=False).model_copy(update={"brightness": 0.5, "motion_score": 0.95, "unusable_reasons": ["transition_spike"]}),
        shot(2, 10, 15, usable=False).model_copy(update={"brightness": 0.05, "unusable_reasons": ["too_dark"], "is_story": False}),
    ]
    plan = plan_candidates(
        shots=invalid,
        start=0,
        end=15,
        needed_duration=5,
        margin=0,
        max_widen=0,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )
    assert plan.candidates == []


def test_dark_fallback_with_zero_min_clip_still_requires_window_intersection() -> None:
    dark_outside = shot(0, 100, 105, usable=False).model_copy(
        update={"brightness": 0.05, "unusable_reasons": ["too_dark"]}
    )

    plan = plan_candidates(
        shots=[dark_outside],
        start=0,
        end=10,
        needed_duration=5,
        margin=0,
        max_widen=0,
        max_clip=5,
        min_visual_clip=0,
        allow_dark_fallback=True,
    )

    assert plan.candidates == []
    assert plan.total_capacity_s == 0
