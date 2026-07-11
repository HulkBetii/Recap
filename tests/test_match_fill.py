from __future__ import annotations

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.fill import Fragment, assign_timeline, avoid_adjacent_repeat_in_tier, fill_beat, fill_timeline_gaps, source_position_for_progress, split_long_placements, trim_fragments_to_duration
from match.scoring import ScoringWeights
from match.timing import validate_source_bounds


def shot(index, start, end, motion=0.5):  # type: ignore[no-untyped-def]
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def test_fill_enforces_max_clip_and_per_beat_duration() -> None:
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=20, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=8, duration=8)
    result = fill_beat(beat=beat, timing=timing, shots=[shot(0,0,10), shot(1,10,20)], reuse_counts={}, weights=ScoringWeights(.6,.18,.12,.35), min_clip=3, max_clip=5, widen_margin=15, max_widen=0, allow_repeat=True, allow_speedfit=False)
    assert all(fragment.duration <= 5.001 for fragment in result.fragments)
    assert abs(sum(fragment.duration for fragment in result.fragments) - 8) < 0.02


def test_fill_uses_dark_local_shots_before_widening() -> None:
    dark_shots = [
        shot(0, 10, 15).model_copy(update={"is_usable": False, "brightness": 0.05, "unusable_reasons": ["too_dark"]}),
        shot(1, 15, 20).model_copy(update={"is_usable": False, "brightness": 0.05, "unusable_reasons": ["too_dark"]}),
    ]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=10, src_tc_end=20, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=8, duration=8)

    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=dark_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=3,
        max_clip=5,
        min_visual_clip=0.6,
        widen_margin=15,
        max_widen=3,
        allow_repeat=True,
        allow_speedfit=False,
    )

    assert result.widen_count == 0
    assert result.dark_selected_ids == [0, 1]
    assert result.overlapping_repeat_count == 0
    assert abs(sum(fragment.duration for fragment in result.fragments) - 8) < 0.02


def test_repeat_uses_uncovered_source_before_overlapping() -> None:
    source_shots = [shot(0, 0, 8)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=8, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=8, duration=8)

    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=3,
        max_clip=5,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
    )

    assert result.unused_source_reuse_count == 1
    assert result.overlapping_repeat_count == 0
    assert [(fragment.src_in, fragment.src_out) for fragment in result.fragments] == [(0.0, 5.0), (5.0, 8.0)]


def test_repeat_overlaps_only_after_source_is_exhausted() -> None:
    source_shots = [shot(0, 0, 8)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=8, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=12, duration=12)

    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=3,
        max_clip=5,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
        max_repeat_per_beat=3,
    )

    assert result.unused_source_reuse_count == 1
    assert result.overlapping_repeat_count == 1
    placements = assign_timeline(result.fragments, timing)
    validate_source_bounds(placements, source_shots)


def test_fill_stays_inside_disjoint_content_anchor_intervals() -> None:
    source_shots = [shot(0, 0, 5), shot(1, 45, 50), shot(2, 90, 95)]
    content_intervals = [(0.0, 10.0), (90.0, 100.0)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=100, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=8, duration=8)

    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=3,
        max_clip=5,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
        match_strategy="chronological",
        ordered_fill=True,
        candidate_filter_ids={0, 2},
        dark_candidate_ids=set(),
        source_intervals=content_intervals,
    )

    assert result.source_intervals == content_intervals
    assert {fragment.shot_index for fragment in result.fragments} == {0, 2}
    assert all(
        any(start <= fragment.src_in < fragment.src_out <= end for start, end in content_intervals)
        for fragment in result.fragments
    )
    assert source_position_for_progress(content_intervals, 0.75) == 95.0
    assert source_position_for_progress(content_intervals, 0.5, weights=[5.0, 15.0]) == 93.33333333333333


def test_repeat_avoids_adjacent_shot_when_same_tier_alternative_exists() -> None:
    ranked = [shot(0, 10, 15), shot(1, 16, 21), shot(2, 40, 45)]

    result = avoid_adjacent_repeat_in_tier(
        ranked,
        previous_shot_index=0,
        source_cursor=10,
        max_source_drift_s=12,
    )

    assert [item.index for item in result] == [1]


def test_assign_timeline_tiles_beat() -> None:
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=10, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=4, duration=4)
    result = fill_beat(beat=beat, timing=timing, shots=[shot(0,0,10)], reuse_counts={}, weights=ScoringWeights(.6,.18,.12,.35), min_clip=3, max_clip=5, widen_margin=15, max_widen=0, allow_repeat=True, allow_speedfit=False)
    placements = assign_timeline(result.fragments, timing)
    assert placements[0].tl_start == 0
    assert placements[-1].tl_end == 4

def test_trim_does_not_extend_across_shot_boundaries() -> None:
    fragments = [
        Fragment(src="film.mp4", src_in=0.0, src_out=5.0, beat_id=0, shot_index=0, reused=False),
        Fragment(src="film.mp4", src_in=10.0, src_out=10.2, beat_id=0, shot_index=1, reused=False),
    ]
    trimmed = trim_fragments_to_duration(fragments, 5.2, min_visual_clip=0.6)
    assert len(trimmed) == 2
    assert trimmed[0].src_out == 5.0
    assert trimmed[1].src_out == 10.2

def test_trim_does_not_expand_next_shot_to_hide_short_lead() -> None:
    fragments = [
        Fragment(src="film.mp4", src_in=0.0, src_out=0.2, beat_id=0, shot_index=0, reused=False),
        Fragment(src="film.mp4", src_in=10.0, src_out=10.8, beat_id=0, shot_index=1, reused=False),
    ]
    trimmed = trim_fragments_to_duration(fragments, 1.0, min_visual_clip=0.6)
    assert len(trimmed) == 2
    assert trimmed[0].src_in == 0.0
    assert trimmed[1].src_out == 10.8

def test_short_pause_gap_extends_previous_placement() -> None:
    placements = [
        assign_timeline([Fragment(src="film.mp4", src_in=0.0, src_out=2.0, beat_id=0, shot_index=0, reused=False)], BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=2, duration=2))[0],
        assign_timeline([Fragment(src="film.mp4", src_in=4.0, src_out=6.0, beat_id=1, shot_index=1, reused=False)], BeatTiming(beat_id=1, audio_path="1.mp3", tl_start=2.15, tl_end=4.15, duration=2))[0],
    ]
    filled = fill_timeline_gaps(placements, 4.15, min_visual_clip=0.6)
    assert len(filled) == 2
    assert filled[0].tl_end == 2.15
    assert round(filled[0].src_out, 3) == 2.0
    assert filled[0].speed >= 0.9
    assert filled[1].tl_start == 2.15


def test_short_pause_uses_source_capacity_without_crossing_shot_end() -> None:
    source_shots = [shot(0, 0, 3), shot(1, 4, 7)]
    placements = [
        assign_timeline([Fragment(src="film.mp4", src_in=0.0, src_out=2.0, beat_id=0, shot_index=0, reused=False)], BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=2, duration=2))[0],
        assign_timeline([Fragment(src="film.mp4", src_in=4.0, src_out=6.0, beat_id=1, shot_index=1, reused=False)], BeatTiming(beat_id=1, audio_path="1.mp3", tl_start=2.15, tl_end=4.15, duration=2))[0],
    ]
    filled = fill_timeline_gaps(placements, 4.15, min_visual_clip=0.6, shots=source_shots)
    assert filled[0].src_out == 2.15
    assert filled[0].speed == 1.0
    validate_source_bounds(filled, source_shots)


def test_short_pause_splits_slowdown_across_adjacent_placements() -> None:
    source_shots = [shot(0, 0, 1.122), shot(1, 10, 15)]
    placements = [
        EdlPlacement(tl_start=0, tl_end=1.122, src="film.mp4", src_in=0, src_out=1.122, beat_id=0, shot_index=0, reused=False, speed=1),
        EdlPlacement(tl_start=1.272, tl_end=6.272, src="film.mp4", src_in=10, src_out=15, beat_id=1, shot_index=1, reused=False, speed=1),
    ]

    filled = fill_timeline_gaps(placements, 6.272, min_visual_clip=0.6, shots=source_shots)

    assert len(filled) == 2
    assert filled[0].tl_end == filled[1].tl_start
    assert filled[0].speed >= 0.9
    assert filled[1].speed >= 0.9
    assert filled[0].src_out == 1.122
    assert filled[1].src_in == 10
    validate_source_bounds(filled, source_shots)


def test_fill_budgets_short_remainder_without_out_of_bounds_extension() -> None:
    source_shots = [shot(0, 0, 5), shot(1, 10, 15)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=15, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=5.2, duration=5.2)
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=3,
        max_clip=5,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
    )
    placements = assign_timeline(result.fragments, timing)
    assert min(item.tl_end - item.tl_start for item in placements) >= 0.6 - 1e-3
    validate_source_bounds(placements, source_shots)


def test_underfilled_beat_uses_source_safe_filler() -> None:
    source_shots = [shot(0, 0, 0.2)]
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=1.0, duration=1.0)
    placements = assign_timeline(
        [Fragment(src="film.mp4", src_in=0.0, src_out=0.2, beat_id=0, shot_index=0, reused=False)],
        timing,
    )
    assert placements[-1].tl_end == 0.2
    filled = fill_timeline_gaps(placements, 1.0, min_visual_clip=0.6, shots=source_shots)
    assert filled[-1].tl_end == 1.0
    assert filled[-1].src_out <= 0.2
    validate_source_bounds(filled, source_shots)


def test_fill_ignores_physical_shots_shorter_than_visual_minimum() -> None:
    source_shots = [shot(0, 0, 1.0), shot(1, 1.0, 1.2)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=1.2, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=1.2, duration=1.2)
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=0.5,
        max_clip=1.0,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
    )
    assert result.fragments
    assert {fragment.shot_index for fragment in result.fragments} == {0}
    assert all(fragment.duration >= 0.6 - 1e-3 for fragment in result.fragments)


def test_fill_leaves_tiny_final_remainder_for_gap_absorption() -> None:
    source_shots = [shot(0, 0, 1.0), shot(1, 1.0, 2.0)]
    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2.0, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=1.071, duration=1.071)
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=source_shots,
        reuse_counts={},
        weights=ScoringWeights(.6, .18, .12, .35),
        min_clip=0.5,
        max_clip=1.0,
        min_visual_clip=0.6,
        widen_margin=0,
        max_widen=0,
        allow_repeat=True,
        allow_speedfit=False,
    )
    placements = assign_timeline(result.fragments, timing)
    filled = fill_timeline_gaps(placements, timing.tl_end, min_visual_clip=0.6, shots=source_shots)
    assert len(filled) == 1
    assert filled[0].tl_end == timing.tl_end
    assert filled[0].speed >= 0.9

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
