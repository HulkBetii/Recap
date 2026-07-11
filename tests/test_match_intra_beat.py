from __future__ import annotations

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.intra_beat import (
    AlignmentChunk,
    coalesce_alignment_chunks,
    estimate_sentence_timings,
    fill_local_window,
    prepare_opening_alignment_sentences,
    recompute_reuse_flags,
    select_monotonic_anchors,
    shared_entity_tokens,
    splice_placements,
)


def beat(*, hook: bool = False, source_end: float = 240.0) -> ReviewBeat:
    return ReviewBeat(
        beat_id=1,
        narration="Câu đầu tiên khá dài. Câu thứ hai tiếp nối. Câu ba ngắn. Câu thứ tư kết thúc.",
        from_seg_id=0,
        to_seg_id=3,
        src_tc_start=0.0,
        src_tc_end=source_end,
        is_hook=hook,
    )


def timing(*, duration: float = 60.0) -> BeatTiming:
    return BeatTiming(beat_id=1, audio_path="audio/1.mp3", tl_start=10.0, tl_end=10.0 + duration, duration=duration)


def shot(index: int, start: float, end: float) -> Shot:
    return Shot(
        src="film.mp4",
        index=index,
        tc_start=start,
        tc_end=end,
        duration=end - start,
        thumb=f"shots/{index}.jpg",
        motion_score=0.5,
        face_count=1,
        face_area=0.1,
        brightness=0.5,
        is_usable=True,
    )


def test_sentence_timings_tile_real_audio_duration() -> None:
    result = estimate_sentence_timings(beat(), timing())

    assert len(result) == 4
    assert result[0].tl_start == 10.0
    assert result[-1].tl_end == 70.0
    assert all(left.tl_end == right.tl_start for left, right in zip(result, result[1:]))


def test_monotonic_dp_never_moves_anchor_backward() -> None:
    current_beat = beat(source_end=30.0)
    current_timing = timing(duration=60.0)
    sentences = estimate_sentence_timings(current_beat, current_timing)[:3]
    shots = [shot(0, 0, 10), shot(1, 10, 20), shot(2, 20, 30)]
    scores = {
        (1, sentences[0].sentence_index, 2): 0.95,
        (1, sentences[1].sentence_index, 0): 0.99,
        (1, sentences[1].sentence_index, 2): 0.80,
        (1, sentences[2].sentence_index, 1): 0.99,
        (1, sentences[2].sentence_index, 2): 0.80,
    }

    chunks = select_monotonic_anchors(
        beat=current_beat,
        timing=current_timing,
        sentences=sentences,
        shots=shots,
        query_shot_scores=scores,
    )

    anchor_ids = [chunk.anchor_shot_index for chunk in chunks]
    assert anchor_ids == sorted(anchor_ids)
    assert anchor_ids[-1] == 2


def test_shared_character_name_is_detected_across_adjacent_sentences() -> None:
    assert shared_entity_tokens(
        "Anh ngồi cạnh Yoo Sang-ah trên tàu.",
        "Sang-ah không muốn tiếp tục cuộc đua.",
    ) == {"sang-ah"}


def test_coalesce_merges_short_sentence_without_leaving_flash_chunk() -> None:
    chunks = [
        AlignmentChunk(1, (0,), "one", 0.0, 4.0, 0, 10.0, 0.8, 5.0),
        AlignmentChunk(1, (1,), "two", 4.0, 6.0, 1, 20.0, 0.8, 12.0),
        AlignmentChunk(1, (2,), "three", 6.0, 10.0, 1, 20.0, 0.8, 18.0),
    ]

    result = coalesce_alignment_chunks(chunks)

    assert [chunk.duration for chunk in result] == [4.0, 6.0]
    assert result[1].sentence_indices == (1, 2)


def test_splice_preserves_timeline_source_mapping_and_untouched_placements() -> None:
    baseline = [
        EdlPlacement(tl_start=0, tl_end=5, src="film.mp4", src_in=0, src_out=5, beat_id=1, shot_index=0, speed=1),
        EdlPlacement(tl_start=5, tl_end=10, src="film.mp4", src_in=5, src_out=10, beat_id=1, shot_index=1, speed=1),
        EdlPlacement(tl_start=10, tl_end=15, src="film.mp4", src_in=10, src_out=15, beat_id=1, shot_index=2, speed=1),
    ]
    replacements = [
        EdlPlacement(tl_start=2, tl_end=5, src="film.mp4", src_in=20, src_out=23, beat_id=1, shot_index=3, speed=1),
        EdlPlacement(tl_start=5, tl_end=8, src="film.mp4", src_in=30, src_out=33, beat_id=1, shot_index=4, speed=1),
    ]

    result = splice_placements(
        baseline_placements=baseline,
        replacements=replacements,
        replaced_ranges=[(2, 8)],
        min_visual_clip=0.6,
    )

    assert [(item.tl_start, item.tl_end) for item in result] == [(0, 2), (2, 5), (5, 8), (8, 10), (10, 15)]
    assert (result[0].src_in, result[0].src_out) == (0, 2)
    assert (result[3].src_in, result[3].src_out) == (8, 10)
    assert result[-1] == baseline[-1]


def test_local_fill_splits_contiguous_source_without_tiny_tail_or_false_repeat() -> None:
    placements = fill_local_window(
        beat_id=1,
        tl_start=35.004,
        tl_end=41.595,
        window_start=372.193,
        window_end=395.271,
        shots=[shot(56, 372.193, 380.178), shot(57, 380.178, 388.164)],
        max_clip=5.0,
        min_visual_clip=0.6,
    )
    placements = recompute_reuse_flags(placements, {})

    assert placements[-1].tl_end == 41.595
    assert all(0.6 <= item.tl_end - item.tl_start <= 5.0 for item in placements)
    assert all(not item.reused for item in placements)


def test_opening_alignment_falls_back_for_hook_approximate_or_tfidf() -> None:
    assert prepare_opening_alignment_sentences(
        beats=[beat(hook=True)],
        timings=[timing()],
        enabled=True,
        semantic_mode="bge-m3",
        strict_timecodes=True,
        opening_guard_s=120,
    ) == {}
    assert prepare_opening_alignment_sentences(
        beats=[beat()],
        timings=[timing()],
        enabled=True,
        semantic_mode="bge-m3",
        strict_timecodes=False,
        opening_guard_s=120,
    ) == {}
    assert prepare_opening_alignment_sentences(
        beats=[beat()],
        timings=[timing()],
        enabled=True,
        semantic_mode="tfidf",
        strict_timecodes=True,
        opening_guard_s=120,
    ) == {}
