from __future__ import annotations

from common.schema import BeatTiming, EdlPlacement, ReviewBeat
from match.sync_qa import build_sync_qa


def test_sync_qa_flags_source_order_and_outside_timing() -> None:
    beats = [ReviewBeat(beat_id=0, narration="beat", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=10, is_hook=True)]
    timings = [BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=4, duration=4)]
    placements = [
        EdlPlacement(tl_start=0, tl_end=2, src="film.mp4", src_in=10, src_out=12, beat_id=0, shot_index=1, reused=False, speed=1),
        EdlPlacement(tl_start=2, tl_end=4.5, src="film.mp4", src_in=5, src_out=7.5, beat_id=0, shot_index=0, reused=True, speed=1),
    ]
    qa = build_sync_qa(beats=beats, timings=timings, placements=placements, fps=30, tolerance_s=0.08)
    beat = qa["beats"][0]
    assert beat["source_order_mismatch"] is True
    assert "source_order_mismatch" in beat["warnings"]
    assert "placement_outside_beat_timing" in beat["warnings"]
    assert qa["summary"]["warning_counts"]["source_order_mismatch"] == 1


def test_sync_qa_clean_tiling_has_no_warnings() -> None:
    beats = [ReviewBeat(beat_id=0, narration="beat", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=4, is_hook=True)]
    timings = [BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=4, duration=4)]
    placements = [EdlPlacement(tl_start=0, tl_end=4, src="film.mp4", src_in=0, src_out=4, beat_id=0, shot_index=0, reused=False, speed=1)]
    qa = build_sync_qa(beats=beats, timings=timings, placements=placements, fps=30)
    assert qa["summary"]["warning_counts"] == {}
    assert qa["beats"][0]["warnings"] == []

def test_sync_qa_flags_short_clip() -> None:
    beats = [ReviewBeat(beat_id=0, narration="beat", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=1, is_hook=True)]
    timings = [BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=1, duration=1)]
    placements = [
        EdlPlacement(tl_start=0, tl_end=0.2, src="film.mp4", src_in=0, src_out=0.2, beat_id=0, shot_index=0, reused=False, speed=1),
        EdlPlacement(tl_start=0.2, tl_end=1, src="film.mp4", src_in=1, src_out=1.8, beat_id=0, shot_index=1, reused=False, speed=1),
    ]
    qa = build_sync_qa(beats=beats, timings=timings, placements=placements, fps=30, short_clip_threshold_s=0.6)
    assert qa["summary"]["warning_counts"]["short_clip"] == 1
    assert qa["beats"][0]["short_clip_count"] == 1
    assert "short_clip" in qa["beats"][0]["warnings"]
