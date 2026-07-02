from __future__ import annotations

from common.schema import FilmMapSegment
from review.coverage import coverage_ratio
from review.timecode import derive_review_beats
from review.models import NarrationBeat, OutlineBeat


def test_derive_review_beats_uses_film_map_timecodes() -> None:
    film_map = [
        FilmMapSegment(id=0, type="speech", tc_start=10.0, tc_end=12.0, ko="a", en="a"),
        FilmMapSegment(id=1, type="speech", tc_start=12.0, tc_end=15.0, ko="b", en="b"),
    ]
    outline = [OutlineBeat(from_seg_id=0, to_seg_id=1, summary="hook", is_hook=True)]
    narration = [NarrationBeat(beat_id=0, narration="Một biến cố mở màn.")]

    beats = derive_review_beats(outline=outline, narration=narration, film_map=film_map)

    assert beats[0].src_tc_start == 10.0
    assert beats[0].src_tc_end == 15.0


def test_coverage_ignores_hook_and_unions_spans() -> None:
    film_map = [
        FilmMapSegment(id=0, type="speech", tc_start=0.0, tc_end=1.0, ko="a", en="a"),
        FilmMapSegment(id=1, type="speech", tc_start=1.0, tc_end=2.0, ko="b", en="b"),
        FilmMapSegment(id=2, type="speech", tc_start=2.0, tc_end=3.0, ko="c", en="c"),
        FilmMapSegment(id=3, type="speech", tc_start=3.0, tc_end=4.0, ko="d", en="d"),
    ]
    outline = [
        OutlineBeat(from_seg_id=3, to_seg_id=3, summary="hook", is_hook=True),
        OutlineBeat(from_seg_id=0, to_seg_id=2, summary="plot"),
        OutlineBeat(from_seg_id=1, to_seg_id=3, summary="plot2"),
    ]
    narration = [
        NarrationBeat(beat_id=0, narration="Hook"),
        NarrationBeat(beat_id=1, narration="A"),
        NarrationBeat(beat_id=2, narration="B"),
    ]

    beats = derive_review_beats(outline=outline, narration=narration, film_map=film_map)

    assert coverage_ratio(beats, len(film_map)) == 1.0
