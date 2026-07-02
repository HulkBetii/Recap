from __future__ import annotations

from common.schema import TranslatedSegment
from ingest.gaps import detect_silent_gaps, select_gaps_for_vision, split_long_gaps


def test_detect_silent_gaps_covers_beginning_middle_end() -> None:
    segments = [
        TranslatedSegment(id=0, tc_start=5.0, tc_end=7.0, ko="a", en="a"),
        TranslatedSegment(id=1, tc_start=12.0, tc_end=13.0, ko="b", en="b"),
    ]

    gaps = detect_silent_gaps(segments, duration=20.0, threshold=4.0)

    assert [(gap.tc_start, gap.tc_end) for gap in gaps] == [(0.0, 5.0), (7.0, 12.0), (13.0, 20.0)]


def test_detect_silent_gaps_filters_threshold() -> None:
    segments = [TranslatedSegment(id=0, tc_start=1.0, tc_end=2.0, ko="a", en="a")]

    gaps = detect_silent_gaps(segments, duration=4.0, threshold=2.0)

    assert gaps == []


def test_select_gaps_for_vision_prioritizes_longest_but_preserves_timeline_order() -> None:
    segments = [
        TranslatedSegment(id=0, tc_start=2.0, tc_end=3.0, ko="a", en="a"),
        TranslatedSegment(id=1, tc_start=10.0, tc_end=11.0, ko="b", en="b"),
        TranslatedSegment(id=2, tc_start=20.0, tc_end=21.0, ko="c", en="c"),
    ]
    gaps = detect_silent_gaps(segments, duration=40.0, threshold=0.5)

    selected = select_gaps_for_vision(gaps, max_frames=2)

    assert [(gap.tc_start, gap.tc_end) for gap in selected] == [(11.0, 20.0), (21.0, 40.0)]


def test_split_long_gaps_keeps_order_and_reassigns_ids() -> None:
    segments = [TranslatedSegment(id=0, tc_start=45.0, tc_end=50.0, ko="a", en="a")]
    gaps = detect_silent_gaps(segments, duration=55.0, threshold=1.0)

    split = split_long_gaps(gaps, max_gap_s=20.0)

    assert [(gap.id, gap.tc_start, gap.tc_end) for gap in split] == [
        (0, 0.0, 20.0),
        (1, 20.0, 40.0),
        (2, 40.0, 45.0),
        (3, 50.0, 55.0),
    ]

def test_split_long_gaps_zero_disables_split_but_reassigns_ids() -> None:
    gaps = detect_silent_gaps([], duration=50.0, threshold=1.0)

    split = split_long_gaps(gaps, max_gap_s=0)

    assert [(gap.id, gap.tc_start, gap.tc_end) for gap in split] == [(0, 0.0, 50.0)]
