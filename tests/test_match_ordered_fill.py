from __future__ import annotations

from common.schema import BeatTiming, ReviewBeat, Shot
from match.fill import fill_beat
from match.scoring import ScoringWeights


def make_shot(index: int, start: float, motion: float) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=start + 4, duration=4, thumb=f"{index}.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def test_ordered_fill_prefers_chronology_over_top_score() -> None:
    beat = ReviewBeat(beat_id=0, narration="opening", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=20, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=6, duration=6)
    shots = [make_shot(0, 0, 0.2), make_shot(1, 10, 0.95)]
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=shots,
        reuse_counts={},
        weights=ScoringWeights(0.6, 0.0, 0.0, 0.0, 0.0),
        min_clip=3,
        max_clip=4,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
        ordered_fill=True,
    )
    assert [fragment.shot_index for fragment in result.fragments] == [0, 1]


def test_ordered_fill_spreads_across_long_source_window() -> None:
    beat = ReviewBeat(beat_id=0, narration="opening", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=100, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=10, duration=10)
    shots = [make_shot(0, 0, 0.5), make_shot(1, 20, 0.5), make_shot(2, 50, 0.5), make_shot(3, 80, 0.5)]
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=shots,
        reuse_counts={},
        weights=ScoringWeights(0.6, 0.0, 0.0, 0.0, 0.0),
        min_clip=2,
        max_clip=2.5,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
        ordered_fill=True,
    )
    assert [fragment.shot_index for fragment in result.fragments] == [0, 1, 2, 3]
