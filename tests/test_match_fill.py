from __future__ import annotations

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.fill import Fragment, assign_timeline, fill_beat, fill_timeline_gaps, split_long_placements, trim_fragments_to_duration
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

def test_trim_coalesces_short_tail_fragment() -> None:
    fragments = [
        Fragment(src="film.mp4", src_in=0.0, src_out=5.0, beat_id=0, shot_index=0, reused=False),
        Fragment(src="film.mp4", src_in=10.0, src_out=10.2, beat_id=0, shot_index=1, reused=False),
    ]
    trimmed = trim_fragments_to_duration(fragments, 5.2, min_visual_clip=0.6)
    assert len(trimmed) == 1
    assert trimmed[0].shot_index == 0
    assert abs(trimmed[0].duration - 5.2) < 0.02

def test_trim_coalesces_short_leading_fragment_into_next() -> None:
    fragments = [
        Fragment(src="film.mp4", src_in=0.0, src_out=0.2, beat_id=0, shot_index=0, reused=False),
        Fragment(src="film.mp4", src_in=10.0, src_out=10.8, beat_id=0, shot_index=1, reused=False),
    ]
    trimmed = trim_fragments_to_duration(fragments, 1.0, min_visual_clip=0.6)
    assert len(trimmed) == 1
    assert trimmed[0].shot_index == 1
    assert abs(trimmed[0].duration - 1.0) < 0.02

def test_short_pause_gap_extends_previous_placement() -> None:
    placements = [
        assign_timeline([Fragment(src="film.mp4", src_in=0.0, src_out=2.0, beat_id=0, shot_index=0, reused=False)], BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=2, duration=2))[0],
        assign_timeline([Fragment(src="film.mp4", src_in=4.0, src_out=6.0, beat_id=1, shot_index=1, reused=False)], BeatTiming(beat_id=1, audio_path="1.mp3", tl_start=2.15, tl_end=4.15, duration=2))[0],
    ]
    filled = fill_timeline_gaps(placements, 4.15, min_visual_clip=0.6)
    assert len(filled) == 2
    assert filled[0].tl_end == 2.15
    assert round(filled[0].src_out, 3) == 2.15
    assert filled[1].tl_start == 2.15

def test_split_long_placement_keeps_continuous_source_and_timeline() -> None:
    placement = EdlPlacement(tl_start=0, tl_end=12, src="film.mp4", src_in=100, src_out=112, beat_id=0, shot_index=3, reused=False, speed=1)
    split = split_long_placements([placement], max_clip=5)
    assert len(split) == 3
    assert all(item.tl_end - item.tl_start <= 5.001 for item in split)
    assert split[0].tl_start == 0
    assert split[-1].tl_end == 12
    assert [item.shot_index for item in split] == [3, 3, 3]
    assert split[0].tl_end == split[1].tl_start
    assert split[1].tl_end == split[2].tl_start
    assert split[0].src_out == split[1].src_in
    assert split[1].src_out == split[2].src_in
