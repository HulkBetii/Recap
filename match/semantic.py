from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from common.schema import FilmMapSegment, ReviewBeat, Shot

TOKEN_RE = re.compile(r"[\w\u00c0-\u1ef9\u3130-\u318f\uac00-\ud7af]+", re.UNICODE)
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"


class SemanticError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemanticConfig:
    mode: str = "off"
    model: str = DEFAULT_EMBEDDING_MODEL
    device: str = "auto"
    batch_size: int = 16
    cache_dir: Path | None = None


@dataclass
class SemanticResult:
    scores: dict[tuple[int, int], float] = field(default_factory=dict)
    ranks: dict[tuple[int, int], int] = field(default_factory=dict)
    provider: str = "off"
    model: str | None = None
    device: str | None = None
    cache_hits: list[str] = field(default_factory=list)


class Encoder(Protocol):
    def encode(self, texts: list[str], *, batch_size: int, device: str) -> list[list[float]]:
        ...


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "") if len(token) > 1]


def segment_text(segment: FilmMapSegment) -> str:
    return " ".join(part for part in (segment.ko, segment.en, segment.scene_desc) if part)


def overlaps(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return start_a < end_b and start_b < end_a


def build_beat_context(beat: ReviewBeat, film_map: list[FilmMapSegment]) -> str:
    parts = [beat.narration]
    for segment in film_map:
        if beat.from_seg_id <= segment.id <= beat.to_seg_id:
            parts.append(segment_text(segment))
    return "\n".join(part for part in parts if part)


def build_shot_context(shot: Shot, film_map: list[FilmMapSegment]) -> str:
    parts = [segment_text(segment) for segment in film_map if overlaps(shot.tc_start, shot.tc_end, segment.tc_start, segment.tc_end)]
    return "\n".join(part for part in parts if part)


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]


def dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def rank_scores(scores: dict[tuple[int, int], float]) -> dict[tuple[int, int], int]:
    by_beat: dict[int, list[tuple[tuple[int, int], float]]] = {}
    for key, score in scores.items():
        by_beat.setdefault(key[0], []).append((key, score))
    ranks: dict[tuple[int, int], int] = {}
    for items in by_beat.values():
        for rank, (key, _score) in enumerate(sorted(items, key=lambda item: (item[1], -item[0][1]), reverse=True), start=1):
            ranks[key] = rank
    return ranks


class TfidfSemanticScorer:
    provider = "tfidf"

    def score(self, beats: list[ReviewBeat], shots: list[Shot], film_map: list[FilmMapSegment]) -> SemanticResult:
        beat_tokens = {beat.beat_id: tokenize(build_beat_context(beat, film_map)) for beat in beats}
        shot_tokens = {shot.index: tokenize(build_shot_context(shot, film_map)) for shot in shots}
        documents = list(beat_tokens.values()) + list(shot_tokens.values())
        document_count = max(1, len(documents))
        document_frequency: Counter[str] = Counter()
        for tokens in documents:
            document_frequency.update(set(tokens))
        idf = {token: math.log((1 + document_count) / (1 + count)) + 1 for token, count in document_frequency.items()}
        beat_vectors = {beat_id: _tfidf_vector(tokens, idf) for beat_id, tokens in beat_tokens.items()}
        shot_vectors = {shot_index: _tfidf_vector(tokens, idf) for shot_index, tokens in shot_tokens.items()}
        scores: dict[tuple[int, int], float] = {}
        for beat_id, beat_vector in beat_vectors.items():
            for shot_index, shot_vector in shot_vectors.items():
                scores[(beat_id, shot_index)] = round(_sparse_cosine(beat_vector, shot_vector), 6)
        return SemanticResult(scores=scores, ranks=rank_scores(scores), provider=self.provider, model="tfidf")


class SentenceTransformerEncoder:
    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SemanticError('semantic-mode=bge-m3 requires optional deps. Install with: pip install -e ".[semantic-embed]"') from exc
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str], *, batch_size: int, device: str) -> list[list[float]]:
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            device=device,
        )
        return [[float(value) for value in row] for row in embeddings]


class EmbeddingSemanticScorer:
    provider = "bge-m3"

    def __init__(self, config: SemanticConfig, encoder: Encoder | None = None) -> None:
        self.config = config
        self.encoder = encoder
        self.cache_hits: list[str] = []
        self.resolved_device = resolve_device(config.device)

    def score(self, beats: list[ReviewBeat], shots: list[Shot], film_map: list[FilmMapSegment]) -> SemanticResult:
        beat_contexts = {beat.beat_id: build_beat_context(beat, film_map) for beat in beats}
        shot_contexts = {shot.index: build_shot_context(shot, film_map) for shot in shots}
        all_keys = [("beat", key) for key in beat_contexts] + [("shot", key) for key in shot_contexts]
        all_texts = [beat_contexts[key] for key in beat_contexts] + [shot_contexts[key] for key in shot_contexts]
        vectors = self._encode_with_cache(all_keys, all_texts)
        beat_vectors = {key[1]: vectors[index] for index, key in enumerate(all_keys) if key[0] == "beat"}
        shot_vectors = {key[1]: vectors[index] for index, key in enumerate(all_keys) if key[0] == "shot"}
        scores: dict[tuple[int, int], float] = {}
        for beat_id, beat_vector in beat_vectors.items():
            for shot_index, shot_vector in shot_vectors.items():
                scores[(beat_id, shot_index)] = round(dense_cosine(beat_vector, shot_vector), 6)
        return SemanticResult(
            scores=scores,
            ranks=rank_scores(scores),
            provider=self.provider,
            model=self.config.model,
            device=self.resolved_device,
            cache_hits=self.cache_hits,
        )

    def _encode_with_cache(self, keys: list[tuple[str, int]], texts: list[str]) -> list[list[float]]:
        vectors: list[list[float] | None] = [None] * len(texts)
        misses: list[tuple[int, str, Path | None]] = []
        cache_dir = self.config.cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(texts):
            path = embedding_cache_path(cache_dir, self.config.model, self.resolved_device, text) if cache_dir else None
            if path is not None and path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                vectors[index] = normalize_vector([float(value) for value in payload["embedding"]])
                self.cache_hits.append(f"semantic/{keys[index][0]}-{keys[index][1]}")
            else:
                misses.append((index, text, path))
        if misses:
            encoder = self.encoder or SentenceTransformerEncoder(self.config.model)
            encoded = encoder.encode([item[1] for item in misses], batch_size=self.config.batch_size, device=self.resolved_device)
            for (index, text, path), vector in zip(misses, encoded):
                normalized = normalize_vector([float(value) for value in vector])
                vectors[index] = normalized
                if path is not None:
                    path.write_text(json.dumps({"model": self.config.model, "device": self.resolved_device, "text_hash": text_hash(text), "embedding": normalized}, ensure_ascii=False), encoding="utf-8")
        return [vector or [] for vector in vectors]


def resolve_device(policy: str) -> str:
    if policy not in {"auto", "cpu", "cuda"}:
        raise SemanticError("--semantic-device must be auto, cpu, or cuda")
    if policy == "cpu":
        return "cpu"
    cuda_available = False
    try:
        import torch
        cuda_available = bool(torch.cuda.is_available())
    except ImportError:
        if policy == "cuda":
            raise SemanticError('semantic-device=cuda requires torch. Install with: pip install -e ".[semantic-embed]"')
    if policy == "cuda" and not cuda_available:
        raise SemanticError("semantic-device=cuda requested but CUDA is not available")
    return "cuda" if cuda_available else "cpu"


def embedding_cache_path(cache_dir: Path | None, model: str, device: str, text: str) -> Path | None:
    if cache_dir is None:
        return None
    key = hashlib.sha256(json.dumps({"model": model, "device": device, "text_hash": text_hash(text)}, sort_keys=True).encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tf(tokens: list[str]) -> Counter[str]:
    return Counter(tokens)


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    counts = _tf(tokens)
    if not counts:
        return {}
    total = sum(counts.values())
    return {token: (count / total) * idf.get(token, 0.0) for token, count in counts.items()}


def _sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(token, 0.0) for token, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def compute_semantic_result(
    beats: list[ReviewBeat],
    shots: list[Shot],
    film_map: list[FilmMapSegment],
    config: SemanticConfig,
    encoder: Encoder | None = None,
) -> SemanticResult:
    if config.mode == "off":
        return SemanticResult(provider="off")
    if config.mode == "tfidf":
        return TfidfSemanticScorer().score(beats, shots, film_map)
    if config.mode == "bge-m3":
        return EmbeddingSemanticScorer(config, encoder=encoder).score(beats, shots, film_map)
    raise SemanticError("--semantic-mode must be off, tfidf, or bge-m3")


def compute_semantic_scores(
    beats: list[ReviewBeat],
    shots: list[Shot],
    film_map: list[FilmMapSegment],
) -> dict[tuple[int, int], float]:
    return TfidfSemanticScorer().score(beats, shots, film_map).scores
