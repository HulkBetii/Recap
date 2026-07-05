from __future__ import annotations

from common.schema import BeatTiming, ReviewBeat, Shot
from match.fill import fill_beat
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

