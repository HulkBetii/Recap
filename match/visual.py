from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from common.schema import ReviewBeat, ReviewIntent, Shot, ShotVisualIndexFile, validate_shot_visual_index
from visual_index.encoder import TransformerVisualEncoder, VisualEncoder, VisualEncoderError, dense_cosine, normalize_vector

class VisualMatchError(RuntimeError):
    pass

@dataclass
class VisualScoreResult:
    scores: dict[tuple[int, int], float] = field(default_factory=dict)
    ranks: dict[tuple[int, int], int] = field(default_factory=dict)
    queries: dict[int, list[str]] = field(default_factory=dict)
    provider: str = "off"
    model: str | None = None
    device: str | None = None
    cache_hits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

def load_visual_index(path: Path, shots: list[Shot]) -> ShotVisualIndexFile:
    index = ShotVisualIndexFile.model_validate_json(path.read_text(encoding="utf-8"))
    return validate_shot_visual_index(index, shots)

def build_visual_queries(beat: ReviewBeat, intent: ReviewIntent | None = None) -> list[str]:
    queries: list[str] = []
    if intent:
        for value in (intent.visual_query_en, intent.visual_query_vi):
            if value:
                queries.append(value)
        cue_parts: list[str] = []
        cue_parts.extend(item.get("name", "") for item in intent.characters)
        cue_parts.extend(intent.action_cues)
        cue_parts.extend(intent.emotion_cues)
        cue_parts.extend(intent.location_cues)
        cue_parts.extend(intent.object_cues)
        if cue_parts:
            queries.append("; ".join(part for part in cue_parts if part))
    queries.append(beat.narration)
    return dedupe(queries)

def dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = " ".join(str(item).split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output

def resolve_ref(index_path: Path, ref: str) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else index_path.parent / path

def load_vector(path: Path) -> list[float]:
    return normalize_vector([float(value) for value in np.load(path).astype("float32").tolist()])

def rank_scores(scores: dict[tuple[int, int], float]) -> dict[tuple[int, int], int]:
    by_beat: dict[int, list[tuple[tuple[int, int], float]]] = {}
    for key, score in scores.items():
        by_beat.setdefault(key[0], []).append((key, score))
    ranks: dict[tuple[int, int], int] = {}
    for items in by_beat.values():
        for rank, (key, _score) in enumerate(sorted(items, key=lambda item: (item[1], -item[0][1]), reverse=True), start=1):
            ranks[key] = rank
    return ranks

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
    shot_vectors: dict[int, list[float]] = {}
    warnings: list[str] = []
    for shot in shots:
        item = by_shot.get(shot.index)
        if item is None:
            warnings.append(f"visual index missing shot {shot.index}")
            continue
        vector_path = resolve_ref(index_path, item.shot_embedding_ref)
        if not vector_path.is_file():
            warnings.append(f"visual embedding missing for shot {shot.index}")
            continue
        shot_vectors[shot.index] = load_vector(vector_path)
    if not shot_vectors:
        raise VisualMatchError("visual index has no usable shot embeddings")

    queries = {beat.beat_id: build_visual_queries(beat, review_intents.get(beat.beat_id)) for beat in beats}
    all_queries = [query for items in queries.values() for query in items]
    query_vectors, cache_hits = encode_queries(
        all_queries,
        cache_dir=cache_dir,
        model=index.meta.embedding_model,
        device=device,
        batch_size=batch_size,
        encoder=encoder,
        trust_remote_code=index.meta.embedding_mode == "jina-clip-v2",
    )
    cursor = 0
    scores: dict[tuple[int, int], float] = {}
    for beat in beats:
        beat_vectors = query_vectors[cursor:cursor + len(queries[beat.beat_id])]
        cursor += len(queries[beat.beat_id])
        for shot_index, shot_vector in shot_vectors.items():
            score = max((dense_cosine(query_vector, shot_vector) for query_vector in beat_vectors), default=0.0)
            scores[(beat.beat_id, shot_index)] = round(score, 6)
    return VisualScoreResult(
        scores=scores,
        ranks=rank_scores(scores),
        queries=queries,
        provider=index.meta.embedding_mode,
        model=index.meta.embedding_model,
        device=device,
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
) -> tuple[list[list[float]], list[str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    vectors: list[list[float] | None] = [None] * len(queries)
    misses: list[tuple[int, str, Path]] = []
    cache_hits: list[str] = []
    for index, query in enumerate(queries):
        path = query_cache_path(cache_dir, model, device, query)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            vectors[index] = normalize_vector([float(value) for value in payload["embedding"]])
            cache_hits.append(f"visual-query/{text_hash(query)[:10]}")
        else:
            misses.append((index, query, path))
    if misses:
        encoder = encoder or TransformerVisualEncoder(model, device=device, trust_remote_code=trust_remote_code)
        encoded = encoder.encode_texts([item[1] for item in misses], batch_size=batch_size)
        for (index, query, path), vector in zip(misses, encoded):
            normalized = normalize_vector(vector)
            vectors[index] = normalized
            path.write_text(json.dumps({"model": model, "device": device, "query_hash": text_hash(query), "embedding": normalized}, ensure_ascii=False), encoding="utf-8")
    return [vector or [] for vector in vectors], cache_hits

def query_cache_path(cache_dir: Path, model: str, device: str, query: str) -> Path:
    key = hashlib.sha256(json.dumps({"model": model, "device": device, "query_hash": text_hash(query)}, sort_keys=True).encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def build_visual_qa(
    *,
    beats: list[ReviewBeat],
    placements,
    visual_result: VisualScoreResult | None,
    visual_mode: str,
) -> dict[str, Any]:
    if visual_result is None:
        return {"version": 1, "visual_mode": visual_mode, "visual_enabled": False, "beats": []}
    placements_by_beat: dict[int, list[Any]] = {}
    for placement in placements:
        placements_by_beat.setdefault(placement.beat_id, []).append(placement)
    beats_report: list[dict[str, Any]] = []
    for beat in beats:
        selected = []
        for placement in placements_by_beat.get(beat.beat_id, []):
            selected.append({
                "shot_index": placement.shot_index,
                "src_in": placement.src_in,
                "src_out": placement.src_out,
                "visual_score": visual_result.scores.get((beat.beat_id, placement.shot_index), 0.0),
                "visual_rank": visual_result.ranks.get((beat.beat_id, placement.shot_index)),
            })
        alternatives = [
            {"shot_index": shot_index, "visual_score": score, "visual_rank": visual_result.ranks.get((beat.beat_id, shot_index))}
            for (beat_id, shot_index), score in visual_result.scores.items()
            if beat_id == beat.beat_id
        ]
        alternatives = sorted(alternatives, key=lambda item: (item["visual_score"], -item["shot_index"]), reverse=True)[:5]
        beats_report.append({
            "beat_id": beat.beat_id,
            "queries": visual_result.queries.get(beat.beat_id, []),
            "selected": selected,
            "alternatives": alternatives,
        })
    return {
        "version": 1,
        "visual_mode": visual_mode,
        "visual_enabled": True,
        "visual_provider": visual_result.provider,
        "visual_model": visual_result.model,
        "visual_device": visual_result.device,
        "visual_cache_hits": visual_result.cache_hits,
        "warnings": visual_result.warnings,
        "beats": beats_report,
    }
