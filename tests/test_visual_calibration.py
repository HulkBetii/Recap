from __future__ import annotations

from common.schema import VisualGoldenBeat, VisualGoldenCandidate, VisualGoldenSet
from match.calibrate_visual import calibrate_visual_weight


def test_calibration_selects_lowest_weight_that_improves_rank_without_regression() -> None:
    golden = VisualGoldenSet(
        videos=["a.mp4", "b.mp4"],
        beats=[
            VisualGoldenBeat(
                video="a.mp4",
                beat_id=0,
                candidates=[
                    VisualGoldenCandidate(shot_index=0, base_score=0.60, visual_score=0.10, acceptable=False),
                    VisualGoldenCandidate(shot_index=1, base_score=0.59, visual_score=0.90, acceptable=True),
                ],
            ),
            VisualGoldenBeat(
                video="b.mp4",
                beat_id=0,
                candidates=[
                    VisualGoldenCandidate(shot_index=2, base_score=0.7, visual_score=0.7, acceptable=True),
                    VisualGoldenCandidate(shot_index=3, base_score=0.2, visual_score=0.1, acceptable=False),
                ],
            ),
        ],
    )
    report = calibrate_visual_weight(golden, weights=(0.0, 0.01, 0.02, 0.05))
    assert report["selected_weight"] == 0.02
    assert report["metrics"][0]["acceptable_top1_rate"] == 0.5
