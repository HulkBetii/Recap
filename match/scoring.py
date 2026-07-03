from __future__ import annotations

from dataclasses import dataclass

from common.schema import Shot


@dataclass(frozen=True)
class ScoringWeights:
    motion: float
    face: float
    bright: float
    reuse: float
    semantic: float = 0.0


def face_bonus(shot: Shot) -> float:
    count_bonus = min(1.0, shot.face_count / 3.0)
    area_bonus = min(1.0, shot.face_area * 4.0)
    return max(count_bonus, area_bonus)


def brightness_bonus(shot: Shot) -> float:
    return max(0.0, 1.0 - abs(shot.brightness - 0.5) * 2.0)


def score_shot(
    shot: Shot,
    reuse_count: int,
    weights: ScoringWeights,
    semantic_score: float = 0.0,
) -> float:
    return (
        weights.motion * shot.motion_score
        + weights.face * face_bonus(shot)
        + weights.bright * brightness_bonus(shot)
        + weights.semantic * semantic_score
        - weights.reuse * reuse_count
    )


def rank_shots(
    shots: list[Shot],
    reuse_counts: dict[int, int],
    weights: ScoringWeights,
    semantic_scores: dict[tuple[int, int], float] | None = None,
    beat_id: int | None = None,
) -> list[Shot]:
    semantic_scores = semantic_scores or {}
    return sorted(
        shots,
        key=lambda shot: (
            score_shot(
                shot,
                reuse_counts.get(shot.index, 0),
                weights,
                semantic_scores.get((beat_id, shot.index), 0.0) if beat_id is not None else 0.0,
            ),
            shot.motion_score,
            -shot.index,
        ),
        reverse=True,
    )
