from __future__ import annotations

import pytest

from common.schema import FilmMapSegment, validate_film_map


def test_validate_film_map_rejects_overlap() -> None:
    segments = [
        FilmMapSegment(id=0, type="speech", tc_start=0.0, tc_end=2.0, ko="안녕", en="Hello"),
        FilmMapSegment(id=1, type="visual", tc_start=1.5, tc_end=3.0, scene_desc="A room."),
    ]

    with pytest.raises(ValueError, match="overlaps"):
        validate_film_map(segments, duration=5.0)


def test_speech_requires_translation() -> None:
    with pytest.raises(ValueError, match="speech segment requires en"):
        FilmMapSegment(id=0, type="speech", tc_start=0.0, tc_end=1.0, ko="안녕")


def test_visual_rejects_text_fields() -> None:
    with pytest.raises(ValueError, match="visual segment requires ko=null"):
        FilmMapSegment(
            id=0,
            type="visual",
            tc_start=0.0,
            tc_end=1.0,
            ko="안녕",
            scene_desc="A person stands outside.",
        )


def test_validate_film_map_rejects_non_continuous_ids() -> None:
    segments = [FilmMapSegment(id=2, type="visual", tc_start=0.0, tc_end=1.0, scene_desc="A car.")]

    with pytest.raises(ValueError, match="continuous"):
        validate_film_map(segments, duration=5.0)
