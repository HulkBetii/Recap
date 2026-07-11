from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from common.schema import ReviewBeat, ReviewIntent, Shot, ShotVisualIndexFile
from visual_index.encoder import TransformerVisualEncoder, VisualEncoder, normalize_vector
from visual_index.integrity import PREPROCESSING_VERSION, resolve_ref, validate_visual_index_artifacts

MAX_QUERY_WORDS = 24


class VisualMatchError(RuntimeError):
    pass


@dataclass
class VisualScoreResult:
    scores: dict[tuple[int, int], float] = field(default_factory=dict)
    raw_cosines: dict[tuple[int, int], float] = field(default_factory=dict)
    ranks: dict[tuple[int, int], int] = field(default_factory=dict)
    queries: dict[int, list[str]] = field(default_factory=dict)
    query_weights: dict[int, list[float]] = field(default_factory=dict)
    selected_keyframes: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    provider: str = "off"
    model: str | None = None
    device: str | None = None
    cache_hits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_visual_index(path: Path, shots: list[Shot]) -> ShotVisualIndexFile:
    index = ShotVisualIndexFile.model_validate_json(path.read_text(encoding="utf-8"))
    return validate_visual_index_artifacts(path, index, shots, require_frames=False, require_calibration=True)


def compact_query(text: str, *, max_words: int = MAX_QUERY_WORDS) -> str:
    return " ".join(" ".join(str(text).split()).split(" ")[:max_words])


def build_visual_queries(beat: ReviewBeat, intent: ReviewIntent | None = None) -> list[str]:
    queries: list[str] = []
    if intent:
        for value in (intent.visual_query_vi, intent.visual_query_en):
            if value:
                queries.append(compact_query(value))
    if not queries:
        queries.append(compact_query(beat.narration))
    return dedupe(queries)[:2]


def visual_query_weights(queries: list[str]) -> list[float]:
    if len(queries) <= 1:
        return [1.0] if queries else []
    return [0.65, 0.35]


def dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = " ".join(str(item).split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def rank_scores(scores: dict[tuple[int, int], float]) -> dict[tuple[int, int], int]:
    by_beat: dict[int, list[tuple[tuple[int, int], float]]] = {}
    for key, score in scores.items():
        by_beat.setdefault(key[0], []).append((key, score))
    ranks: dict[tuple[int, int], int] = {}
    for items in by_beat.values():
        for rank, (key, _score) in enumerate(sorted(items, key=lambda item: (item[1], -item[0][1]), reverse=True), start=1):
            ranks[key] = rank
    return ranks


def calibrated_probability(cosine: np.ndarray, *, logit_scale: float, logit_bias: float) -> np.ndarray:
    logits = np.clip(cosine * logit_scale + logit_bias, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-logits))


def compute_visual_scores(
    *,
    beats: list[ReviewBeat],
    shots: list[Shot],
    review_intents: dict[int, ReviewIntent],
    index_path: Path,
    cache_dir: Path,
    device: str,
    batch_size: int,
    encoder: VisualEncoder | None = None,
) -> VisualScoreResult:
    index = load_visual_index(index_path, shots)
    by_shot = {item.shot_index: item for item in index.shots}
    warnings: list[str] = []
    frame_vectors: list[list[float]] = []
    frame_meta: list[tuple[int, Any]] = []
    frame_positions_by_shot: dict[int, list[int]] = {}
    for shot in shots:
        item = by_shot.get(shot.index)
        if item is None:
            warnings.append(f"visual index missing shot {shot.index}")
            continue
        for keyframe in item.keyframes:
            vector_path = resolve_ref(index_path, keyframe.embedding_ref)
            vector = normalize_vector(np.load(vector_path, allow_pickle=False).astype("float32").tolist())
            position = len(frame_vectors)
            frame_vectors.append(vector)
            frame_meta.append((shot.index, keyframe))
            frame_positions_by_shot.setdefault(shot.index, []).append(position)
    if not frame_vectors:
        raise VisualMatchError("visual index has no usable keyframe embeddings")

    queries = {beat.beat_id: build_visual_queries(beat, review_intents.get(beat.beat_id)) for beat in beats}
    query_weights = {beat_id: visual_query_weights(items) for beat_id, items in queries.items()}
    unique_queries = dedupe([query for items in queries.values() for query in items])
    unique_vectors, cache_hits, actual_device = encode_queries(
        unique_queries,
        cache_dir=cache_dir,
        model=index.meta.embedding_model,
        device=device,
        batch_size=batch_size,
        encoder=encoder,
        trust_remote_code=index.meta.embedding_mode == "jina-clip-v2",
        expected_dim=index.meta.embedding_dim,
    )
    vector_by_query = {query.casefold(): vector for query, vector in zip(unique_queries, unique_vectors)}
    query_matrix = np.asarray(
        [vector_by_query[query.casefold()] for beat in beats for query in queries[beat.beat_id]],
        dtype=np.float32,
    )
    frame_matrix = np.asarray(frame_vectors, dtype=np.float32)
    cosine_matrix = np.matmul(query_matrix, frame_matrix.T)
    probability_matrix = calibrated_probability(
        cosine_matrix,
        logit_scale=float(index.meta.logit_scale),
        logit_bias=float(index.meta.logit_bias),
    )

    cursor = 0
    scores: dict[tuple[int, int], float] = {}
    raw_cosines: dict[tuple[int, int], float] = {}
    selected_keyframes: dict[tuple[int, int], dict[str, Any]] = {}
    for beat in beats:
        count = len(queries[beat.beat_id])
        beat_cosines = cosine_matrix[cursor:cursor + count]
        beat_probabilities = probability_matrix[cursor:cursor + count]
        weights = np.asarray(query_weights[beat.beat_id], dtype=np.float32)
        cursor += count
        for shot in shots:
            positions = frame_positions_by_shot.get(shot.index, [])
            if not positions:
                continue
            per_frame_score = np.matmul(weights, beat_probabilities[:, positions])
            per_frame_cosine = np.matmul(weights, beat_cosines[:, positions])
            best_local = int(np.argmax(per_frame_score))
            frame_position = positions[best_local]
            _shot_index, keyframe = frame_meta[frame_position]
            score = float(per_frame_score[best_local])
            raw_cosine = float(per_frame_cosine[best_local])
            scores[(beat.beat_id, shot.index)] = round(score, 6)
            raw_cosines[(beat.beat_id, shot.index)] = round(raw_cosine, 6)
            selected_keyframes[(beat.beat_id, shot.index)] = {
                "frame_path": str(resolve_ref(index_path, keyframe.frame_path)),
                "frame_ref": keyframe.frame_path,
                "tc": keyframe.tc,
                "role": keyframe.role,
                "raw_cosine": round(raw_cosine, 6),
                "probability": round(score, 6),
            }
    return VisualScoreResult(
        scores=scores,
        raw_cosines=raw_cosines,
        ranks=rank_scores(scores),
        queries=queries,
        query_weights=query_weights,
        selected_keyframes=selected_keyframes,
        provider=index.meta.embedding_mode,
        model=index.meta.embedding_model,
        device=actual_device,
        cache_hits=cache_hits,
        warnings=warnings,
    )


def encode_queries(
    queries: list[str],
    *,
    cache_dir: Path,
    model: str,
    device: str,
    batch_size: int,
    encoder: VisualEncoder | None,
    trust_remote_code: bool,
    expected_dim: int,
) -> tuple[list[list[float]], list[str], str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    vectors: list[list[float] | None] = [None] * len(queries)
    misses: list[tuple[int, str, Path]] = []
    cache_hits: list[str] = []
    for index, query in enumerate(queries):
        path = query_cache_path(cache_dir, model, device, query)
        payload = None
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                vector = normalize_vector([float(value) for value in payload["embedding"]])
                if payload.get("preprocessing_version") != PREPROCESSING_VERSION or len(vector) != expected_dim:
                    payload = None
                elif not all(math.isfinite(value) for value in vector):
                    payload = None
                else:
                    vectors[index] = vector
                    cache_hits.append(f"visual-query/{text_hash(query)[:10]}")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                payload = None
        if payload is None:
            misses.append((index, query, path))
    actual_device = device
    if misses:
        encoder = encoder or TransformerVisualEncoder(model, device=device, trust_remote_code=trust_remote_code)
        actual_device = encoder.device
        encoded = encoder.encode_texts([item[1] for item in misses], batch_size=batch_size)
        if len(encoded) != len(misses):
            raise VisualMatchError(f"visual encoder returned {len(encoded)} vectors for {len(misses)} queries")
        for (index, query, path), vector in zip(misses, encoded):
            normalized = normalize_vector(vector)
            if len(normalized) != expected_dim:
                raise VisualMatchError(f"visual query embedding dimension {len(normalized)} != index dimension {expected_dim}")
            vectors[index] = normalized
            path.write_text(
                json.dumps(
                    {
                        "model": model,
                        "device": actual_device,
                        "preprocessing_version": PREPROCESSING_VERSION,
                        "query_hash": text_hash(query),
                        "embedding": normalized,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    return [vector or [] for vector in vectors], cache_hits, actual_device


def query_cache_path(cache_dir: Path, model: str, device: str, query: str) -> Path:
    key = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "device": device,
                "preprocessing_version": PREPROCESSING_VERSION,
                "query_hash": text_hash(query),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return cache_dir / f"{key}.json"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_visual_qa(
    *,
    beats: list[ReviewBeat],
    placements: list[Any],
    visual_result: VisualScoreResult | None,
    visual_mode: str,
    candidate_shot_ids: dict[int, list[int]] | None = None,
    combined_scores: dict[tuple[int, int], float] | None = None,
    candidate_drift_tiers: dict[tuple[int, int], int] | None = None,
) -> dict[str, Any]:
    if visual_result is None:
        return {"version": 2, "visual_mode": visual_mode, "visual_enabled": False, "beats": []}
    candidate_shot_ids = candidate_shot_ids or {}
    combined_scores = combined_scores or {}
    candidate_drift_tiers = candidate_drift_tiers or {}
    placements_by_beat: dict[int, list[Any]] = {}
    for placement in placements:
        placements_by_beat.setdefault(placement.beat_id, []).append(placement)
    beats_report: list[dict[str, Any]] = []
    raw_cosines = getattr(visual_result, "raw_cosines", {})
    query_weights = getattr(visual_result, "query_weights", {})
    selected_keyframes = getattr(visual_result, "selected_keyframes", {})
    for beat in beats:
        selected = []
        for placement in placements_by_beat.get(beat.beat_id, []):
            key = (beat.beat_id, placement.shot_index)
            selected.append(
                {
                    "shot_index": placement.shot_index,
                    "src_in": placement.src_in,
                    "src_out": placement.src_out,
                    "raw_cosine": raw_cosines.get(key, 0.0),
                    "visual_score": visual_result.scores.get(key, 0.0),
                    "visual_rank": visual_result.ranks.get(key),
                    "combined_score": combined_scores.get(key),
                    "selected_keyframe": selected_keyframes.get(key),
                    "drift_tier": candidate_drift_tiers.get(key),
                }
            )
        allowed = set(candidate_shot_ids.get(beat.beat_id, []))
        alternatives = []
        for (beat_id, shot_index), score in visual_result.scores.items():
            if beat_id != beat.beat_id or (beat.beat_id in candidate_shot_ids and shot_index not in allowed):
                continue
            key = (beat_id, shot_index)
            alternatives.append(
                {
                    "shot_index": shot_index,
                    "raw_cosine": raw_cosines.get(key, 0.0),
                    "visual_score": score,
                    "visual_rank": visual_result.ranks.get(key),
                    "combined_score": combined_scores.get(key),
                    "selected_keyframe": selected_keyframes.get(key),
                    "drift_tier": candidate_drift_tiers.get(key),
                }
            )
        alternatives = sorted(
            alternatives,
            key=lambda item: (
                item["combined_score"] if item["combined_score"] is not None else float("-inf"),
                item["visual_score"],
                -item["shot_index"],
            ),
            reverse=True,
        )[:5]
        beats_report.append(
            {
                "beat_id": beat.beat_id,
                "queries": visual_result.queries.get(beat.beat_id, []),
                "query_weights": query_weights.get(beat.beat_id, []),
                "selected": selected,
                "alternatives": alternatives,
            }
        )
    return {
        "version": 2,
        "visual_mode": visual_mode,
        "visual_enabled": True,
        "visual_provider": visual_result.provider,
        "visual_model": visual_result.model,
        "visual_device": visual_result.device,
        "visual_cache_hits": visual_result.cache_hits,
        "warnings": visual_result.warnings,
        "beats": beats_report,
    }
