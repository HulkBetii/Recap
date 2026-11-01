from __future__ import annotations

import json
from pathlib import Path

from common.schema import BeatTiming, ReviewBeat, Shot
from match.__main__ import build_parser, run_match
from match.fill import fill_beat, fill_timeline_gaps
from match.scoring import ScoringWeights


def shot(index: int, start: float, end: float, *, is_story: bool, motion: float) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=is_story, is_story=is_story, exclude_reason=None if is_story else "intro_opening")


def test_fill_beat_does_not_select_non_story_shot_when_filtered() -> None:
    beat = ReviewBeat(beat_id=0, narration="story", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=10, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=3, duration=3)
    shots = [shot(1, 0, 5, is_story=True, motion=0.4)]
    result = fill_beat(beat=beat, timing=timing, shots=shots, reuse_counts={}, weights=ScoringWeights(0.6,0.18,0.12,0.35,0.0), min_clip=3, max_clip=5, widen_margin=0, max_widen=0, allow_repeat=True, allow_speedfit=False, semantic_scores={})
    assert {fragment.shot_index for fragment in result.fragments} == {1}


def test_pause_filler_reuses_previous_story_placement() -> None:
    # Pause filler takes the previous selected placement; because GĐ5 filters non-story candidates first,
    # the filler should remain on a story shot.
    beat = ReviewBeat(beat_id=0, narration="story", from_seg_id=0, to_seg_id=0, src_tc_start=5, src_tc_end=10, is_hook=False)
    timing = BeatTiming(beat_id=0, audio_path="0.mp3", tl_start=0, tl_end=2, duration=2)
    result = fill_beat(beat=beat, timing=timing, shots=[shot(2,5,8,is_story=True,motion=0.5)], reuse_counts={}, weights=ScoringWeights(0.6,0.18,0.12,0.35,0.0), min_clip=2, max_clip=5, widen_margin=0, max_widen=0, allow_repeat=True, allow_speedfit=False, semantic_scores={})
    from match.fill import assign_timeline
    placements = assign_timeline(result.fragments, timing)
    filled = fill_timeline_gaps(placements, 2.5)
    assert filled[-1].shot_index == 2

def test_run_match_excludes_anime_non_story_shots(tmp_path: Path) -> None:
    review_script = tmp_path / "review_script.json"
    beats_timing = tmp_path / "beats_timing.json"
    shots_path = tmp_path / "shots.json"
    output = tmp_path / "edl.json"
    review_script.write_text(
        json.dumps(
            [
                {
                    "beat_id": 0,
                    "narration": "Aki nhận ra manh mối trong thành phố.",
                    "from_seg_id": 0,
                    "to_seg_id": 0,
                    "src_tc_start": 0,
                    "src_tc_end": 4,
                    "is_hook": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    beats_timing.write_text(
        json.dumps([{"beat_id": 0, "audio_path": "0.mp3", "tl_start": 0, "tl_end": 3, "duration": 3}]),
        encoding="utf-8",
    )
    shots_path.write_text(
        json.dumps(
            [
                {
                    "src": "anime.mp4",
                    "index": 0,
                    "tc_start": 0,
                    "tc_end": 4,
                    "duration": 4,
                    "thumb": "story.jpg",
                    "motion_score": 0.1,
                    "face_count": 0,
                    "face_area": 0,
                    "brightness": 0.4,
                    "is_usable": True,
                    "is_story": True,
                },
                {
                    "src": "anime.mp4",
                    "index": 1,
                    "tc_start": 0,
                    "tc_end": 4,
                    "duration": 4,
                    "thumb": "opening.jpg",
                    "motion_score": 1.0,
                    "face_count": 3,
                    "face_area": 0.5,
                    "brightness": 0.8,
                    "is_usable": False,
                    "is_story": False,
                    "exclude_reason": "opening_theme",
                },
            ]
        ),
        encoding="utf-8",
    )

    args = build_parser().parse_args(
        [
            "--review-script",
            str(review_script),
            "--beats-timing",
            str(beats_timing),
            "--shots",
            str(shots_path),
            "--output",
            str(output),
            "--semantic-mode",
            "off",
            "--min-clip",
            "3",
            "--max-clip",
            "5",
            "--work-dir",
            str(tmp_path / "work"),
            "--no-review-html",
        ]
    )

    assert run_match(args) == 0
    edl = json.loads(output.read_text(encoding="utf-8"))
    meta = json.loads(output.with_name("edl.meta.json").read_text(encoding="utf-8"))
    assert {item["shot_index"] for item in edl} == {0}
    assert meta["n_intro_excluded"] == 1
