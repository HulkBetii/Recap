from __future__ import annotations

import pytest

from common.schema import FilmMapSegment, ReviewBeat, validate_review_script


def sample_film_map() -> list[FilmMapSegment]:
    return [
        FilmMapSegment(id=0, type="speech", tc_start=0.0, tc_end=1.0, ko="a", en="a"),
        FilmMapSegment(id=1, type="speech", tc_start=1.0, tc_end=2.0, ko="b", en="b"),
        FilmMapSegment(id=2, type="visual", tc_start=2.0, tc_end=3.0, scene_desc="c"),
    ]


def test_validate_review_script_rejects_mismatched_timecode() -> None:
    beats = [
        ReviewBeat(
            beat_id=0,
            narration="Hook",
            from_seg_id=1,
            to_seg_id=1,
            src_tc_start=1.5,
            src_tc_end=2.5,
            is_hook=True,
        )
    ]

    with pytest.raises(ValueError, match="src_tc_start"):
        validate_review_script(beats, sample_film_map())


def test_validate_review_script_rejects_non_monotonic_non_hook() -> None:
    beats = [
        ReviewBeat(beat_id=0, narration="Hook", from_seg_id=2, to_seg_id=2, src_tc_start=2.0, src_tc_end=3.0, is_hook=True),
        ReviewBeat(beat_id=1, narration="Later", from_seg_id=1, to_seg_id=1, src_tc_start=1.0, src_tc_end=2.0),
        ReviewBeat(beat_id=2, narration="Earlier", from_seg_id=0, to_seg_id=0, src_tc_start=0.0, src_tc_end=1.0),
    ]

    with pytest.raises(ValueError, match="monotonic"):
        validate_review_script(beats, sample_film_map())


def test_validate_review_script_requires_hook_first() -> None:
    beats = [ReviewBeat(beat_id=0, narration="Start", from_seg_id=0, to_seg_id=0, src_tc_start=0.0, src_tc_end=1.0)]

    with pytest.raises(ValueError, match="first beat"):
        validate_review_script(beats, sample_film_map())

