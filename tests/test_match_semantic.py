from __future__ import annotations

from common.schema import EdlPlacement, FilmMapSegment, ReviewBeat, Shot
from match.qa import build_edl_qa
from match.scoring import ScoringWeights, score_shot
from match.semantic import SemanticConfig, build_beat_context, build_shot_context, compute_semantic_result, compute_semantic_scores


def seg(id: int, start: float, end: float, text: str) -> FilmMapSegment:
    return FilmMapSegment(id=id, type="speech", tc_start=start, tc_end=end, ko=text, en=text, scene_desc=None)


def shot(index: int, start: float, end: float, motion: float = 0.5) -> Shot:
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=motion, face_count=0, face_area=0, brightness=0.5, is_usable=True)


def beat() -> ReviewBeat:
    return ReviewBeat(beat_id=0, narration="anh hùng cứu công chúa khỏi cung điện", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=10, is_hook=True)


def test_context_builders_use_source_span_and_overlap() -> None:
    film_map = [seg(0, 0, 5, "hero palace"), seg(1, 6, 9, "unrelated kitchen")]
    assert "hero palace" in build_beat_context(beat(), film_map)
    assert "unrelated" not in build_beat_context(beat(), film_map)
    assert "hero palace" in build_shot_context(shot(0, 4, 6), film_map)
    assert "unrelated" not in build_shot_context(shot(0, 4, 6), film_map)


def test_tfidf_scores_related_shot_higher() -> None:
    film_map = [seg(0, 0, 5, "hero princess palace rescue"), seg(1, 5, 10, "cars rain street")]
    scores = compute_semantic_scores([beat()], [shot(0, 0, 5), shot(1, 5, 10)], film_map)
    assert scores[(0, 0)] > scores[(0, 1)]


def test_tfidf_reports_narration_to_segment_anchor_scores() -> None:
    film_map = [seg(0, 0, 5, "anh hùng cứu công chúa"), seg(1, 5, 10, "xe chạy trong mưa")]
    result = compute_semantic_result(
        [beat().model_copy(update={"to_seg_id": 1})],
        [shot(0, 0, 5), shot(1, 5, 10)],
        film_map,
        SemanticConfig(mode="tfidf"),
    )
    assert result.segment_scores[(0, 0)] > result.segment_scores[(0, 1)]


def test_semantic_weight_can_change_ranking() -> None:
    weights = ScoringWeights(motion=0.6, face=0.0, bright=0.0, reuse=0.0, semantic=0.7)
    high_motion = shot(0, 0, 5, motion=0.8)
    related = shot(1, 5, 10, motion=0.2)
    assert score_shot(related, 0, weights, semantic_score=1.0) > score_shot(high_motion, 0, weights, semantic_score=0.0)


def test_edl_qa_warns_on_low_semantic() -> None:
    placement = EdlPlacement(tl_start=0, tl_end=2, src="film.mp4", src_in=0, src_out=2, beat_id=0, shot_index=0, reused=False, speed=1.0)
    qa = build_edl_qa(
        beats=[beat()],
        placements=[placement],
        shots=[shot(0, 0, 5)],
        semantic_scores={(0, 0): 0.01},
        weights=ScoringWeights(0.6, 0.18, 0.12, 0.35, 0.35),
        min_semantic_score=0.12,
        warnings=[],
    )
    assert qa["beats"][0]["selected"][0]["semantic_score"] == 0.01
    assert "low semantic match" in qa["beats"][0]["warnings"][0]

def test_edl_qa_reports_source_drift() -> None:
    placement = EdlPlacement(tl_start=5, tl_end=7, src="film.mp4", src_in=80, src_out=82, beat_id=0, shot_index=1, reused=False, speed=1.0)
    qa = build_edl_qa(
        beats=[ReviewBeat(beat_id=0, narration="opening", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=100, is_hook=True)],
        placements=[placement],
        shots=[shot(1, 80, 84)],
        semantic_scores={(0, 1): 0.9},
        weights=ScoringWeights(0.6, 0.18, 0.12, 0.35, 0.35),
        min_semantic_score=0.12,
        warnings=[],
        match_strategy="hybrid",
        max_source_drift_s=12,
    )
    selected = qa["beats"][0]["selected"][0]
    assert selected["expected_src_position"] == 0
    assert selected["source_drift_s"] == 80
    assert selected["chronology_score"] == 0
    assert any("high source drift" in warning for warning in qa["beats"][0]["warnings"])
    assert "semantic overrode chronology" in qa["beats"][0]["warnings"]


def test_edl_qa_uses_content_anchor_intervals_for_expected_position() -> None:
    placements = [
        EdlPlacement(tl_start=0, tl_end=4, src="film.mp4", src_in=0, src_out=4, beat_id=0, shot_index=0, reused=False, speed=1.0),
        EdlPlacement(tl_start=6, tl_end=10, src="film.mp4", src_in=92, src_out=96, beat_id=0, shot_index=1, reused=False, speed=1.0),
    ]
    qa = build_edl_qa(
        beats=[ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=100, is_hook=False)],
        placements=placements,
        shots=[shot(0, 0, 5), shot(1, 90, 100)],
        semantic_scores={},
        weights=ScoringWeights(0.6, 0.18, 0.12, 0.35, 0.0),
        min_semantic_score=0.12,
        warnings=[],
        candidate_diagnostics={0: {"content_anchor_intervals": [[0, 10], [90, 100]], "content_anchor_interval_weights": [10, 10], "content_anchor_used": True}},
    )

    assert qa["beats"][0]["selected"][1]["expected_src_position"] == 92.0
    assert qa["beats"][0]["selected"][1]["source_drift_s"] == 0.0


from pathlib import Path

import pytest

from match.semantic import EmbeddingSemanticScorer, SemanticError, resolve_device


class MockEncoder:
    def __init__(self) -> None:
        self.calls = 0
        self.texts: list[str] = []

    def encode(self, texts: list[str], *, batch_size: int, device: str) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "c?ng ch?a" in lowered or "princess" in lowered or "hero" in lowered:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def test_embedding_scorer_ranks_cross_lingual_context(tmp_path: Path) -> None:
    film_map = [seg(0, 0, 5, "hero princess palace rescue"), seg(1, 5, 10, "cars rain street")]
    encoder = MockEncoder()
    result = compute_semantic_result(
        [beat().model_copy(update={"narration": "hero rescues princess", "to_seg_id": 1})],
        [shot(0, 0, 5), shot(1, 5, 10)],
        film_map,
        SemanticConfig(mode="bge-m3", model="mock", device="cpu", cache_dir=tmp_path),
        encoder=encoder,
    )
    assert result.provider == "bge-m3"
    assert result.device == "cpu"
    assert result.scores[(0, 0)] > result.scores[(0, 1)]
    assert result.ranks[(0, 0)] == 1
    assert result.segment_scores[(0, 0)] > result.segment_scores[(0, 1)]


def test_embedding_cache_skips_reencode(tmp_path: Path) -> None:
    film_map = [seg(0, 0, 5, "hero princess palace rescue")]
    encoder = MockEncoder()
    config = SemanticConfig(mode="bge-m3", model="mock", device="cpu", cache_dir=tmp_path)
    compute_semantic_result([beat()], [shot(0, 0, 5)], film_map, config, encoder=encoder)
    first_calls = encoder.calls
    second = compute_semantic_result([beat()], [shot(0, 0, 5)], film_map, config, encoder=encoder)
    assert encoder.calls == first_calls
    assert second.cache_hits


def test_embedding_scorer_skips_segment_encoding_when_anchors_are_disabled(tmp_path: Path) -> None:
    encoder = MockEncoder()
    result = compute_semantic_result(
        [beat()],
        [shot(0, 0, 5)],
        [seg(0, 0, 5, "hero princess palace rescue")],
        SemanticConfig(mode="bge-m3", model="mock", device="cpu", cache_dir=tmp_path, score_segments=False),
        encoder=encoder,
    )

    assert result.segment_scores == {}
    assert len(encoder.texts) == 2


def test_embedding_scorer_batches_alignment_queries_with_shot_contexts(tmp_path: Path) -> None:
    encoder = MockEncoder()
    result = compute_semantic_result(
        [beat()],
        [shot(0, 0, 5), shot(1, 5, 10)],
        [seg(0, 0, 5, "hero princess palace rescue")],
        SemanticConfig(mode="bge-m3", model="mock", device="cpu", cache_dir=tmp_path, score_segments=False),
        encoder=encoder,
        alignment_queries={(0, 3): "hero rescues princess"},
    )

    assert result.query_shot_scores[(0, 3, 0)] > result.query_shot_scores[(0, 3, 1)]
    assert "hero rescues princess" in encoder.texts


def test_bge_missing_dependency_fails_clearly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "sentence_transformers":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    scorer = EmbeddingSemanticScorer(SemanticConfig(mode="bge-m3", model="mock", device="cpu", cache_dir=tmp_path))
    with pytest.raises(SemanticError, match="semantic-embed"):
        scorer.score([beat()], [shot(0, 0, 5)], [seg(0, 0, 5, "hero")])


def test_resolve_device_cpu_and_torch_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "torch":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") == "cpu"
    with pytest.raises(SemanticError, match="requires torch"):
        resolve_device("cuda")


def test_resolve_device_cuda_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    assert resolve_device("auto") == "cpu"
    with pytest.raises(SemanticError, match="CUDA"):
        resolve_device("cuda")
