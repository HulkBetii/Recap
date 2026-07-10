from __future__ import annotations

from pathlib import Path

import numpy as np

from common.schema import BeatTiming, ReviewBeat, ReviewIntent, Shot
from match.fill import fill_beat
from match.scoring import ScoringWeights, rank_shots
from match.visual import build_visual_qa, compute_visual_scores

class FakeTextEncoder:
    device = "cpu"

    def encode_texts(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        return [[0.0, 1.0] for _text in texts]

def make_shot(index: int, start: float, end: float) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb=f"{index}.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True)

def write_visual_index(tmp_path: Path) -> Path:
    emb_dir = tmp_path / "visual_index" / "emb"
    emb_dir.mkdir(parents=True)
    np.save(emb_dir / "shot0.npy", np.asarray([1.0, 0.0], dtype=np.float16))
    np.save(emb_dir / "shot1.npy", np.asarray([0.0, 1.0], dtype=np.float16))
    path = tmp_path / "shot_visual_index.json"
    path.write_text(
        """
{
  "meta": {
    "version": "1.0",
    "src": "film.mp4",
    "embedding_mode": "siglip2",
    "embedding_model": "mock",
    "device": "cpu",
    "embedding_dim": 2,
    "keyframes_per_shot": 1,
    "n_shots": 2,
    "created_at": "2026-07-02T00:00:00Z",
    "cache_hits": [],
    "warnings": []
  },
  "shots": [
    {
      "shot_index": 0,
      "tc_start": 0,
      "tc_end": 2,
      "duration": 2,
      "is_story": true,
      "is_usable": true,
      "keyframes": [{"frame_path": "visual_index/frames/0.jpg", "tc": 1, "role": "mid", "embedding_ref": "visual_index/emb/shot0.npy"}],
      "shot_embedding_ref": "visual_index/emb/shot0.npy"
    },
    {
      "shot_index": 1,
      "tc_start": 2,
      "tc_end": 4,
      "duration": 2,
      "is_story": true,
      "is_usable": true,
      "keyframes": [{"frame_path": "visual_index/frames/1.jpg", "tc": 3, "role": "mid", "embedding_ref": "visual_index/emb/shot1.npy"}],
      "shot_embedding_ref": "visual_index/emb/shot1.npy"
    }
  ]
}
""",
        encoding="utf-8",
    )
    return path

def test_visual_scores_rank_matching_shot_higher(tmp_path: Path) -> None:
    shots = [make_shot(0, 0, 2), make_shot(1, 2, 4)]
    beat = ReviewBeat(beat_id=0, narration="woman sees the secret", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=4, is_hook=True)
    result = compute_visual_scores(
        beats=[beat],
        shots=shots,
        review_intents={0: ReviewIntent(beat_id=0, visual_query_en="secret reaction")},
        index_path=write_visual_index(tmp_path),
        cache_dir=tmp_path / "cache",
        device="cpu",
        batch_size=2,
        encoder=FakeTextEncoder(),
    )
    assert result.scores[(0, 1)] > result.scores[(0, 0)]
    ranked = rank_shots(shots, {}, ScoringWeights(0, 0, 0, 0, visual=1.0), visual_scores=result.scores, beat_id=0)
    assert ranked[0].index == 1

def test_chronological_fill_keeps_time_prior_over_visual_score() -> None:
    shots = [make_shot(0, 0, 2), make_shot(1, 2, 4)]
    beat = ReviewBeat(beat_id=0, narration="woman sees the secret", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=4, is_hook=True)
    timing = BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0, tl_end=1, duration=1)
    result = fill_beat(
        beat=beat,
        timing=timing,
        shots=shots,
        reuse_counts={},
        weights=ScoringWeights(0, 0, 0, 0, visual=1.0),
        min_clip=0.5,
        max_clip=1.0,
        widen_margin=0,
        max_widen=0,
        allow_repeat=False,
        allow_speedfit=False,
        visual_scores={(0, 0): 0.0, (0, 1): 1.0},
        match_strategy="chronological",
        max_source_drift_s=12.0,
    )
    assert result.fragments[0].shot_index == 0

def test_visual_qa_reports_selected_and_alternatives() -> None:
    beat = ReviewBeat(beat_id=0, narration="A", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2, is_hook=True)
    placement = type("Placement", (), {"beat_id": 0, "shot_index": 1, "src_in": 2, "src_out": 3})()
    qa = build_visual_qa(
        beats=[beat],
        placements=[placement],
        visual_result=type("Result", (), {
            "scores": {(0, 0): 0.1, (0, 1): 0.9},
            "ranks": {(0, 1): 1, (0, 0): 2},
            "queries": {0: ["query"]},
            "provider": "siglip2",
            "model": "mock",
            "device": "cpu",
            "cache_hits": [],
            "warnings": [],
        })(),
        visual_mode="rerank",
    )
    assert qa["visual_enabled"] is True
    assert qa["beats"][0]["selected"][0]["visual_score"] == 0.9
    assert qa["beats"][0]["alternatives"][0]["shot_index"] == 1
