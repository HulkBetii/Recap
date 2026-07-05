from __future__ import annotations

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.fill import fill_beat
from match.qa import build_edl_qa
from match.scoring import ScoringWeights


def shot(index: int, start: float, end: float, motion: float = 0.5) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb=f"s{index}.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def beat() -> ReviewBeat:
    return ReviewBeat(beat_id=0, narration="C?u chuy?n b?t ??u trong m?t c?n nh? l?.", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=4, is_hook=True)


def test_opening_guard_short_fills_instead_of_repeat_when_disabled() -> None:
    result = fill_beat(
        beat=beat(),
        timing=BeatTiming(beat_id=0, audio_path="a.mp3", tl_start=0, tl_end=8, duration=8),
        shots=[shot(0, 0, 4, 0.9)],
        reuse_counts={},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_clip=3,
        max_clip=5,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
        max_repeat_per_beat=1,
        max_repeat_ratio_per_beat=0.2,
    )
    assert len(result.fragments) == 1
    assert any("could not fill" in warning for warning in result.warnings)


def test_qa_flags_opening_repeat_confusing() -> None:
    placements = [
        EdlPlacement(tl_start=0, tl_end=3, src="film.mp4", src_in=0, src_out=3, beat_id=0, shot_index=0, reused=False, speed=1),
        EdlPlacement(tl_start=3, tl_end=6, src="film.mp4", src_in=0, src_out=3, beat_id=0, shot_index=0, reused=True, speed=1),
    ]
    qa = build_edl_qa(
        beats=[beat()],
        placements=placements,
        shots=[shot(0, 0, 4)],
        semantic_scores={},
        weights=ScoringWeights(1, 0, 0, 0, 0),
        min_semantic_score=0.1,
        warnings=[],
        max_repeat_ratio_per_beat=0.35,
        opening_guard_s=120,
        opening_max_repeat_ratio=0.2,
        opening_min_unique_shots=2,
    )
    warnings = qa["beats"][0]["warnings"]
    assert "opening_repeat_confusing" in warnings
    assert any("opening_low_unique_shots" in warning for warning in warnings)
