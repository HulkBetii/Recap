from __future__ import annotations

import pytest

from common.schema import EdlPlacement, validate_edl


def test_edl_rejects_non_1_to_1() -> None:
    with pytest.raises(ValueError, match="1:1"):
        EdlPlacement(tl_start=0, tl_end=2, src="film.mp4", src_in=0, src_out=1, beat_id=0, shot_index=0, reused=False, speed=1.0)


def test_validate_edl_rejects_gap() -> None:
    edl = [
        EdlPlacement(tl_start=0, tl_end=1, src="film.mp4", src_in=0, src_out=1, beat_id=0, shot_index=0, reused=False, speed=1.0),
        EdlPlacement(tl_start=1.2, tl_end=2.2, src="film.mp4", src_in=2, src_out=3, beat_id=1, shot_index=1, reused=False, speed=1.0),
    ]
    with pytest.raises(ValueError, match="gap"):
        validate_edl(edl, total_duration=2.2)
