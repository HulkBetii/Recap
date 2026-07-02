from __future__ import annotations

from common.schema import BeatTiming, ReviewBeat, Shot
from match.fill import assign_timeline, fill_beat
from match.scoring import ScoringWeights


def shot(index, start, end, motion=0.5):  # type: ignore[no-untyped-def]
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def test_fill_enforces_max_clip_and_per_beat_duration() -> None:
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=20, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=8, duration=8)
    result = fill_beat(beat=beat, timing=timing, shots=[shot(0,0,10), shot(1,10,20)], reuse_counts={}, weights=ScoringWeights(.6,.18,.12,.35), min_clip=3, max_clip=5, widen_margin=15, max_widen=0, allow_repeat=True, allow_speedfit=False)
    assert all(fragment.duration <= 5.001 for fragment in result.fragments)
    assert abs(sum(fragment.duration for fragment in result.fragments) - 8) < 0.02


def test_assign_timeline_tiles_beat() -> None:
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=10, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=4, duration=4)
    result = fill_beat(beat=beat, timing=timing, shots=[shot(0,0,10)], reuse_counts={}, weights=ScoringWeights(.6,.18,.12,.35), min_clip=3, max_clip=5, widen_margin=15, max_widen=0, allow_repeat=True, allow_speedfit=False)
    placements = assign_timeline(result.fragments, timing)
    assert placements[0].tl_start == 0
    assert placements[-1].tl_end == 4
