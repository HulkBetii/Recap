from __future__ import annotations

from common.schema import BeatTiming, ReviewBeat, SentenceRef, SentenceTimestamp, Shot
from match.scoring import ScoringWeights
from match.sentence_fill import fill_beat_by_sentences, plan_sentence_windows


def shot(index: int, start: float, end: float) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end - start, thumb="x.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True)


SHOTS = [shot(index, index * 5.0, index * 5.0 + 5.0) for index in range(20)]
BEAT = ReviewBeat(beat_id=0, narration="Xe lật giữa đèo. Trước đó anh bị bắt! Thật trớ trêu.", from_seg_id=0, to_seg_id=9, src_tc_start=0.0, src_tc_end=100.0)
TIMING = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=10.0, tl_end=22.0, duration=12.0)
STAMPS = [
    SentenceTimestamp(beat_id=0, idx=0, tl_start=10.0, tl_end=14.0, method="silence"),
    SentenceTimestamp(beat_id=0, idx=1, tl_start=14.0, tl_end=18.0, method="silence"),
    SentenceTimestamp(beat_id=0, idx=2, tl_start=18.0, tl_end=22.0, method="silence"),
]
REFS = [
    SentenceRef(beat_id=0, idx=0, text="Xe lật giữa đèo.", from_seg_id=7, to_seg_id=7, src_tc_start=70.0, src_tc_end=76.0, grounded=True),
    SentenceRef(beat_id=0, idx=1, text="Trước đó anh bị bắt!", from_seg_id=1, to_seg_id=1, src_tc_start=10.0, src_tc_end=16.0, grounded=True),
    SentenceRef(beat_id=0, idx=2, text="Thật trớ trêu.", grounded=False),
]
FILL_KWARGS = dict(
    shots=SHOTS,
    weights=ScoringWeights(0.6, 0.18, 0.12, 0.35),
    min_clip=3,
    max_clip=5,
    min_visual_clip=0.6,
    widen_margin=15,
    max_widen=3,
    allow_repeat=True,
    allow_speedfit=False,
    ordered_fill=True,
    ordered_fill_by_audio_progress=True,
    match_strategy="chronological",
)


def test_each_sentence_is_shown_over_its_own_source_moment() -> None:
    windows, reason = plan_sentence_windows(BEAT, TIMING, REFS, STAMPS)
    assert reason is None and windows is not None
    result, placements, diagnostics = fill_beat_by_sentences(beat=BEAT, timing=TIMING, windows=windows, reuse_counts={}, fill_kwargs=FILL_KWARGS)

    first = [p for p in placements if p.tl_start < 14.0]
    second = [p for p in placements if 14.0 <= p.tl_start < 18.0]
    assert first and all(66.0 <= p.src_in and p.src_out <= 80.0 for p in first)
    assert second and all(6.0 <= p.src_in and p.src_out <= 20.0 for p in second)
    # The commentary sentence borrows footage between its neighbours' moments, inside the beat span.
    third = [p for p in placements if p.tl_start >= 18.0]
    assert third and all(0.0 <= p.src_in and p.src_out <= 100.0 for p in third)
    # The beat's audio window is tiled exactly with clips no longer than max_clip.
    assert placements[0].tl_start == 10.0 and abs(placements[-1].tl_end - 22.0) < 1e-6
    assert all(abs(a.tl_end - b.tl_start) < 1e-6 for a, b in zip(placements, placements[1:]))
    assert all(p.tl_end - p.tl_start <= 5.0 + 1e-6 for p in placements)
    assert [item["ref_hit_ratio"] for item in diagnostics if "ref_hit_ratio" in item] == [1.0, 1.0]
    assert result.required_duration_s == 12.0


def test_short_ref_window_is_widened_inside_the_beat_span() -> None:
    refs = [REFS[0].model_copy(update={"src_tc_start": 98.0, "src_tc_end": 99.0}), REFS[1], REFS[2]]
    windows, _reason = plan_sentence_windows(BEAT, TIMING, refs, STAMPS)
    assert windows is not None
    assert windows[0].src_end == 100.0 and windows[0].src_end - windows[0].src_start >= 6.0 - 1e-6


def test_mismatched_or_ungrounded_sentences_fall_back_to_beat_level() -> None:
    assert plan_sentence_windows(BEAT, TIMING, REFS[:2], STAMPS)[0] is None
    ungrounded = [SentenceRef(beat_id=0, idx=index, text=ref.text) for index, ref in enumerate(REFS)]
    windows, reason = plan_sentence_windows(BEAT, TIMING, ungrounded, STAMPS)
    assert windows is None and "no sentence" in reason


def test_sentences_citing_the_same_segment_do_not_repeat_footage() -> None:
    same = [
        REFS[0],
        REFS[0].model_copy(update={"idx": 1, "text": "Trước đó anh bị bắt!"}),
        REFS[2],
    ]
    windows, _reason = plan_sentence_windows(BEAT, TIMING, same, STAMPS)
    assert windows is not None
    _result, placements, _diagnostics = fill_beat_by_sentences(beat=BEAT, timing=TIMING, windows=windows, reuse_counts={}, fill_kwargs=FILL_KWARGS)
    shown: list[tuple[float, float]] = []
    for placement in placements:
        assert all(placement.src_out <= start + 1e-6 or placement.src_in >= end - 1e-6 for start, end in shown)
        shown.append((placement.src_in, placement.src_out))
    second = [p for p in placements if 14.0 <= p.tl_start < 18.0]
    assert second and all(60.0 <= p.src_in <= 90.0 for p in second)


def test_sentence_visual_scores_pick_the_matching_shot_inside_the_window() -> None:
    refs = [REFS[0].model_copy(update={"src_tc_start": 60.0, "src_tc_end": 76.0}), REFS[1], REFS[2]]
    windows, _reason = plan_sentence_windows(BEAT, TIMING, refs, STAMPS)
    assert windows is not None
    plain = fill_beat_by_sentences(beat=BEAT, timing=TIMING, windows=windows, reuse_counts={}, fill_kwargs=FILL_KWARGS)[1]
    assert plain[0].shot_index == 12  # chronological fill starts at the window start
    scores = {(0, 0): {12: 0.0, 13: 0.1, 14: 1.0, 15: 0.2}}
    _result, placements, diagnostics = fill_beat_by_sentences(
        beat=BEAT,
        timing=TIMING,
        windows=windows,
        reuse_counts={},
        fill_kwargs=FILL_KWARGS,
        sentence_visual_scores=scores,
        visual_weight=0.6,
    )
    first = [p for p in placements if p.tl_start < 14.0]
    assert first[0].shot_index == 14
    assert diagnostics[0]["visual_scored"] is True and diagnostics[1]["visual_scored"] is False
