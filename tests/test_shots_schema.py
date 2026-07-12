from __future__ import annotations

import pytest

from common.schema import Shot, validate_shots


def make_shot(index: int, start: float, end: float) -> Shot:
    return Shot(
        src="film.mp4",
        index=index,
        tc_start=start,
        tc_end=end,
        duration=end - start,
        thumb=f"shots/{index}.jpg",
        motion_score=0.5,
        face_count=0,
        face_area=0.0,
        brightness=0.5,
        is_usable=True,
    )


def test_shot_rejects_invalid_score() -> None:
    with pytest.raises(ValueError):
        Shot(
            src="film.mp4",
            index=0,
            tc_start=0.0,
            tc_end=1.0,
            duration=1.0,
            thumb="shots/0.jpg",
            motion_score=1.5,
            face_count=0,
            face_area=0.0,
            brightness=0.5,
            is_usable=True,
        )


def test_shot_rejects_duration_mismatch() -> None:
    with pytest.raises(ValueError, match="duration"):
        Shot(
            src="film.mp4",
            index=0,
            tc_start=0.0,
            tc_end=1.0,
            duration=2.0,
            thumb="shots/0.jpg",
            motion_score=0.5,
            face_count=0,
            face_area=0.0,
            brightness=0.5,
            is_usable=True,
        )


def test_validate_shots_requires_continuous_index_and_bounds() -> None:
    shots = [make_shot(0, 0.0, 1.0), make_shot(2, 1.0, 2.0)]

    with pytest.raises(ValueError, match="continuous"):
        validate_shots(shots, duration=3.0)

    with pytest.raises(ValueError, match="exceeds"):
        validate_shots([make_shot(0, 0.0, 4.0)], duration=3.0)


def test_shot_unusable_reasons_are_optional_and_normalized() -> None:
    legacy = make_shot(0, 0.0, 1.0)
    assert legacy.unusable_reasons == []
    assert legacy.is_end_credit is False
    assert legacy.credit_like_score == 0.0

    dark = legacy.model_copy(update={"is_usable": False, "unusable_reasons": [" too_dark ", "too_dark"]})
    reparsed = Shot.model_validate(dark.model_dump())
    assert reparsed.unusable_reasons == ["too_dark"]
