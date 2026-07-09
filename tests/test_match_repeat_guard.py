from __future__ import annotations

from common.schema import BeatTiming, ReviewBeat, Shot
from match.fill import fill_beat, fill_timeline_gaps
from match.scoring import ScoringWeights


def shot(index: int, start: float, end: float, motion: float = 0.5) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb=f"s{index}.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def make_beat() -> ReviewBeat:
    return ReviewBeat(beat_id=0, narration="beat", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=8, is_hook=True)


def test_repeat_guard_uses_alternative_before_adjacent_repeat() -> None:
    result = fill_beat(
        beat=make_beat(),
        timing=BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=8, duration=8),
        shots=[shot(0, 0, 4, 0.9), shot(1, 4, 8, 0.85)],
        reuse_counts={},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_clip=3,
        max_clip=5,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
        max_repeat_per_beat=2,
        max_repeat_ratio_per_beat=0.9,
    )

    assert [fragment.shot_index for fragment in result.fragments[:2]] == [0, 1]


def test_near_repeat_guard_uses_good_recent_alternative() -> None:
    result = fill_beat(
        beat=make_beat(),
        timing=BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=5, tl_end=9, duration=4),
        shots=[shot(0, 0, 4, 0.95), shot(1, 4, 8, 0.8)],
        reuse_counts={0: 1},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_clip=3,
        max_clip=4,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
        avoid_recent_shot_indexes={0},
        near_repeat_min_alternative_score_ratio=0.65,
    )

    assert result.fragments[0].shot_index == 1
    assert not any("near_repeat_guard could not avoid" in warning for warning in result.warnings)

def test_high_repeat_ratio_warning_when_fallback_repeats() -> None:
    result = fill_beat(
        beat=make_beat(),
        timing=BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=12, duration=12),
        shots=[shot(0, 0, 3, 0.9)],
        reuse_counts={},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_clip=3,
        max_clip=5,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
        max_repeat_per_beat=1,
        max_repeat_ratio_per_beat=0.35,
    )

    assert any("high repeat ratio" in warning for warning in result.warnings)


def test_long_gap_filler_uses_next_shot_lead_in_to_avoid_visible_repeat() -> None:
    from common.schema import EdlPlacement

    placements = [
        EdlPlacement(tl_start=0, tl_end=1.8, src="film.mp4", src_in=29.5, src_out=31.3, beat_id=0, shot_index=1, reused=False, speed=1.0),
        EdlPlacement(tl_start=5.5, tl_end=8.0, src="film.mp4", src_in=37.3, src_out=39.8, beat_id=1, shot_index=2, reused=False, speed=1.0),
    ]

    filled = fill_timeline_gaps(placements, total_duration=8.0)

    assert [placement.shot_index for placement in filled] == [1, 2, 2]
    assert filled[1].tl_start == 1.8
    assert filled[1].tl_end == 5.5
    assert filled[1].src_out == 37.3
    assert filled[1].src_in < filled[1].src_out

def test_empty_placements_warning_when_no_candidates() -> None:
    result = fill_beat(
        beat=make_beat(),
        timing=BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=4, duration=4),
        shots=[],
        reuse_counts={},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_clip=3,
        max_clip=5,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
    )

    assert result.fragments == []
    assert any("empty beat placements" in warning for warning in result.warnings)

