from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat
from match.__main__ import opening_story_visual_start


def beat(start: float = 0.0, end: float = 180.0) -> ReviewBeat:
    return ReviewBeat(
        beat_id=0,
        narration="Mở đầu phim là khu chợ cá.",
        from_seg_id=0,
        to_seg_id=2,
        src_tc_start=start,
        src_tc_end=end,
        is_hook=True,
    )


def test_opening_story_visual_start_skips_logo_and_credits() -> None:
    film_map = [
        FilmMapSegment(id=0, type="speech", tc_start=0, tc_end=4, ko="Shoebox.", en="Shoebox."),
        FilmMapSegment(id=1, type="visual", tc_start=12, tc_end=40, scene_desc="opening credits on a black screen with white text"),
        FilmMapSegment(id=2, type="visual", tc_start=45.085, tc_end=60, scene_desc="Two men unload boxes in a busy fish market."),
    ]
    assert opening_story_visual_start(beat(), film_map) == 45.085


def test_opening_story_visual_start_does_not_override_later_beats() -> None:
    film_map = [FilmMapSegment(id=0, type="visual", tc_start=45, tc_end=60, scene_desc="A busy market.")]
    assert opening_story_visual_start(beat(start=40, end=90), film_map) is None


def test_opening_story_visual_start_ignores_late_visuals() -> None:
    film_map = [FilmMapSegment(id=0, type="visual", tc_start=120, tc_end=130, scene_desc="A busy market.")]
    assert opening_story_visual_start(beat(), film_map) is None
