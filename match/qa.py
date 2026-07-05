from __future__ import annotations

from collections import defaultdict
from typing import Any

from common.schema import EdlPlacement, ReviewBeat, Shot
from match.scoring import brightness_bonus, face_bonus, score_shot, ScoringWeights
from match.semantic import SemanticResult


def narration_preview(text: str, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def build_edl_qa(
    *,
    beats: list[ReviewBeat],
    placements: list[EdlPlacement],
    shots: list[Shot],
    semantic_scores: dict[tuple[int, int], float],
    weights: ScoringWeights,
    semantic_result: SemanticResult | None = None,
    min_semantic_score: float,
    warnings: list[str],
) -> dict[str, Any]:
    shots_by_index = {shot.index: shot for shot in shots}
    placements_by_beat: dict[int, list[EdlPlacement]] = defaultdict(list)
    for placement in placements:
        placements_by_beat[placement.beat_id].append(placement)
    warning_by_beat: dict[int, list[str]] = defaultdict(list)
    for warning in warnings:
        for beat in beats:
            if f"beat {beat.beat_id} " in warning or warning.endswith(f"beat {beat.beat_id}"):
                warning_by_beat[beat.beat_id].append(warning)
                break
    beat_reports: list[dict[str, Any]] = []
    for beat in beats:
        selected = []
        beat_semantic_values: list[float] = []
        for placement in placements_by_beat.get(beat.beat_id, []):
            shot = shots_by_index.get(placement.shot_index)
            semantic_score = semantic_scores.get((beat.beat_id, placement.shot_index), 0.0)
            beat_semantic_values.append(semantic_score)
            entry: dict[str, Any] = {
                "tl_start": placement.tl_start,
                "tl_end": placement.tl_end,
                "src_in": placement.src_in,
                "src_out": placement.src_out,
                "shot_index": placement.shot_index,
                "reused": placement.reused,
                "semantic_score": round(semantic_score, 6),
                "semantic_rank": semantic_result.ranks.get((beat.beat_id, placement.shot_index)) if semantic_result else None,
            }
            if shot is not None:
                entry.update({
                    "motion_score": shot.motion_score,
                    "brightness": shot.brightness,
                    "brightness_bonus": round(brightness_bonus(shot), 6),
                    "face_count": shot.face_count,
                    "face_area": shot.face_area,
                    "face_bonus": round(face_bonus(shot), 6),
                    "total_score_no_reuse": round(score_shot(shot, 0, weights, semantic_score), 6),
                    "is_story": shot.is_story,
                    "exclude_reason": shot.exclude_reason,
                    "selected_from_non_story": not shot.is_story,
                })
            selected.append(entry)
        avg_semantic = sum(beat_semantic_values) / len(beat_semantic_values) if beat_semantic_values else 0.0
        beat_warnings = list(warning_by_beat.get(beat.beat_id, []))
        if beat_semantic_values and avg_semantic < min_semantic_score:
            beat_warnings.append(f"low semantic match: avg={avg_semantic:.3f} < {min_semantic_score:.3f}")
        beat_reports.append({
            "beat_id": beat.beat_id,
            "narration_preview": narration_preview(beat.narration),
            "source_window": {"start": beat.src_tc_start, "end": beat.src_tc_end},
            "avg_semantic_score": round(avg_semantic, 6),
            "selected": selected,
            "warnings": beat_warnings,
        })
    excluded_intro = [shot.index for shot in shots if not shot.is_story]
    return {
        "version": 3,
        "semantic_enabled": bool(semantic_scores),
        "semantic_provider": semantic_result.provider if semantic_result else ("tfidf" if semantic_scores else "off"),
        "semantic_model": semantic_result.model if semantic_result else None,
        "semantic_device": semantic_result.device if semantic_result else None,
        "semantic_cache_hits": semantic_result.cache_hits if semantic_result else [],
        "min_semantic_score": min_semantic_score,
        "n_intro_excluded": len(excluded_intro),
        "excluded_intro_candidates": excluded_intro[:200],
        "selected_from_non_story": any((not shots_by_index.get(item.shot_index, Shot(src="unknown", index=0, tc_start=0, tc_end=1, duration=1, thumb="unknown", motion_score=0, face_count=0, face_area=0, brightness=0, is_usable=False)).is_story) for item in placements),
        "beats": beat_reports,
    }
