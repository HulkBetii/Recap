from __future__ import annotations

from collections import defaultdict
from typing import Any

from common.schema import EdlPlacement, ReviewBeat, ReviewIntent, Shot, StorySection
from match.scoring import brightness_bonus, face_bonus, score_shot, ScoringWeights
from match.semantic import SemanticResult
from match.visual import VisualScoreResult
from match.fill import chronology_tier, source_position_for_progress


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
    visual_scores: dict[tuple[int, int], float] | None = None,
    semantic_result: SemanticResult | None = None,
    visual_result: VisualScoreResult | None = None,
    min_semantic_score: float,
    warnings: list[str],
    max_repeat_ratio_per_beat: float = 0.35,
    opening_guard_s: float = 0.0,
    opening_max_repeat_ratio: float = 0.20,
    opening_min_unique_shots: int = 4,
    review_intents: dict[int, ReviewIntent] | None = None,
    story_sections: dict[int, StorySection] | None = None,
    match_strategy: str = "hybrid",
    max_source_drift_s: float = 12.0,
    short_clip_threshold_s: float = 0.0,
    candidate_shot_ids: dict[int, list[int]] | None = None,
    candidate_drift_tiers: dict[tuple[int, int], int] | None = None,
    candidate_diagnostics: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    review_intents = review_intents or {}
    story_sections = story_sections or {}
    visual_scores = visual_scores or {}
    candidate_shot_ids = candidate_shot_ids or {}
    candidate_drift_tiers = candidate_drift_tiers or {}
    candidate_diagnostics = candidate_diagnostics or {}
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
        candidate_info = candidate_diagnostics.get(beat.beat_id, {})
        dark_candidate_ids = {int(item) for item in candidate_info.get("dark_candidate_ids", [])}
        intent = review_intents.get(beat.beat_id)
        section = story_sections.get(intent.story_section_id) if intent and intent.story_section_id is not None else None
        selected = []
        beat_semantic_values: list[float] = []
        beat_selected = placements_by_beat.get(beat.beat_id, [])
        beat_tl_start = min((placement.tl_start for placement in beat_selected), default=0.0)
        beat_tl_end = max((placement.tl_end for placement in beat_selected), default=0.0)
        beat_tl_span = max(0.001, beat_tl_end - beat_tl_start)
        beat_src_span = max(0.001, beat.src_tc_end - beat.src_tc_start)
        anchor_intervals = [
            (float(item[0]), float(item[1]))
            for item in candidate_info.get("content_anchor_intervals", [])
            if isinstance(item, (list, tuple)) and len(item) == 2 and float(item[1]) > float(item[0])
        ]
        anchor_interval_weights = [float(item) for item in candidate_info.get("content_anchor_interval_weights", [])]
        intra_beat_chunks = [
            item
            for item in candidate_info.get("opening_intra_beat_chunks", [])
            if isinstance(item, dict) and item.get("replaced")
        ]
        beat_drifts: list[float] = []
        semantic_override = False
        for placement in beat_selected:
            shot = shots_by_index.get(placement.shot_index)
            semantic_score = semantic_scores.get((beat.beat_id, placement.shot_index), 0.0)
            visual_score = visual_scores.get((beat.beat_id, placement.shot_index), 0.0)
            beat_semantic_values.append(semantic_score)
            tl_progress = min(1.0, max(0.0, (placement.tl_start - beat_tl_start) / beat_tl_span))
            placement_midpoint = (placement.tl_start + placement.tl_end) / 2
            intra_beat_chunk = next(
                (
                    item
                    for item in intra_beat_chunks
                    if (
                        float((item.get("replacement_range") or [item.get("tl_start", 0.0), item.get("tl_end", 0.0)])[0]) - 1e-6
                        <= placement_midpoint
                        <= float((item.get("replacement_range") or [item.get("tl_start", 0.0), item.get("tl_end", 0.0)])[1]) + 1e-6
                    )
                ),
                None,
            )
            if intra_beat_chunk is not None:
                replacement_range = intra_beat_chunk.get("replacement_range") or [intra_beat_chunk["tl_start"], intra_beat_chunk["tl_end"]]
                chunk_tl_start = float(replacement_range[0])
                chunk_tl_end = float(replacement_range[1])
                source_window = intra_beat_chunk.get("source_window", [])
                chunk_progress = min(1.0, max(0.0, (placement.tl_start - chunk_tl_start) / max(chunk_tl_end - chunk_tl_start, 0.001)))
                expected_src_position = source_position_for_progress(
                    [(float(source_window[0]), float(source_window[1]))],
                    chunk_progress,
                )
            else:
                expected_src_position = (
                    source_position_for_progress(anchor_intervals, tl_progress, weights=anchor_interval_weights)
                    if anchor_intervals
                    else beat.src_tc_start + beat_src_span * tl_progress
                )
            source_drift_s = source_drift(placement, expected_src_position)
            beat_drifts.append(source_drift_s)
            chronology_score = max(0.0, 1.0 - source_drift_s / max(max_source_drift_s, 0.001))
            if source_drift_s > max_source_drift_s and semantic_score >= 0.5:
                semantic_override = True
            entry: dict[str, Any] = {
                "tl_start": placement.tl_start,
                "tl_end": placement.tl_end,
                "duration_s": round(placement.tl_end - placement.tl_start, 3),
                "src_in": placement.src_in,
                "src_out": placement.src_out,
                "expected_src_position": round(expected_src_position, 3),
                "source_drift_s": round(source_drift_s, 3),
                "chronology_score": round(chronology_score, 6),
                "shot_index": placement.shot_index,
                "reused": placement.reused,
                "semantic_score": round(semantic_score, 6),
                "semantic_rank": semantic_result.ranks.get((beat.beat_id, placement.shot_index)) if semantic_result else None,
                "visual_score": round(visual_score, 6),
                "visual_raw_cosine": visual_result.raw_cosines.get((beat.beat_id, placement.shot_index)) if visual_result else None,
                "visual_rank": visual_result.ranks.get((beat.beat_id, placement.shot_index)) if visual_result else None,
                "selected_keyframe": visual_result.selected_keyframes.get((beat.beat_id, placement.shot_index)) if visual_result else None,
                "drift_tier": chronology_tier(shot, expected_src_position, max_source_drift_s=max_source_drift_s)[0] if shot else None,
                "dark_fallback": placement.shot_index in dark_candidate_ids,
                "opening_intra_beat_chunk": intra_beat_chunk,
            }
            if shot is not None:
                entry.update({
                    "motion_score": shot.motion_score,
                    "brightness": shot.brightness,
                    "brightness_bonus": round(brightness_bonus(shot), 6),
                    "face_count": shot.face_count,
                    "face_area": shot.face_area,
                    "face_bonus": round(face_bonus(shot), 6),
                    "total_score_no_reuse": round(score_shot(shot, 0, weights, semantic_score, visual_score), 6),
                    "is_story": shot.is_story,
                    "exclude_reason": shot.exclude_reason,
                    "selected_from_non_story": not shot.is_story,
                })
            selected.append(entry)
        n_reused = sum(1 for placement in beat_selected if placement.reused)
        repeat_ratio = n_reused / len(beat_selected) if beat_selected else 0.0
        unique_shots = len({placement.shot_index for placement in beat_selected})
        clip_durations = [max(0.0, placement.tl_end - placement.tl_start) for placement in beat_selected]
        min_clip_s = min(clip_durations) if clip_durations else 0.0
        short_clip_count = sum(
            1
            for duration_s in clip_durations
            if short_clip_threshold_s > 0 and duration_s + 1e-3 < short_clip_threshold_s
        )
        avg_semantic = sum(beat_semantic_values) / len(beat_semantic_values) if beat_semantic_values else 0.0
        beat_warnings = list(warning_by_beat.get(beat.beat_id, []))
        if not beat_selected:
            beat_warnings.append("empty beat placements")
        in_opening_guard = opening_guard_s > 0 and any(placement.tl_start < opening_guard_s for placement in beat_selected)
        repeat_limit = opening_max_repeat_ratio if in_opening_guard else max_repeat_ratio_per_beat
        if beat_selected and repeat_ratio > repeat_limit:
            beat_warnings.append(f"high repeat ratio: {repeat_ratio:.3f} > {repeat_limit:.3f}")
        if in_opening_guard and beat_selected and repeat_ratio > opening_max_repeat_ratio:
            beat_warnings.append("opening_repeat_confusing")
        if in_opening_guard and beat_selected and unique_shots < opening_min_unique_shots:
            beat_warnings.append(f"opening_low_unique_shots: {unique_shots} < {opening_min_unique_shots}")
        if in_opening_guard and any("opening_short_fill" in warning for warning in beat_warnings):
            beat_warnings.append("opening_short_fill")
        ordered_fill_used = in_opening_guard and any("opening_ordered_fill" in warning for warning in beat_warnings)
        chronology_mismatch = False
        if in_opening_guard and beat_selected:
            src_order = [placement.src_in for placement in beat_selected]
            chronology_mismatch = any(src_order[index] > src_order[index + 1] + 1e-3 for index in range(len(src_order) - 1))
            if chronology_mismatch:
                beat_warnings.append("chronology_mismatch")
        if beat_semantic_values and avg_semantic < min_semantic_score:
            beat_warnings.append(f"low semantic match: avg={avg_semantic:.3f} < {min_semantic_score:.3f}")
        if beat_semantic_values and avg_semantic < min_semantic_score and repeat_ratio > repeat_limit:
            beat_warnings.append("low semantic + high reuse")
        max_source_drift = max(beat_drifts) if beat_drifts else 0.0
        avg_source_drift = sum(beat_drifts) / len(beat_drifts) if beat_drifts else 0.0
        if beat_drifts and max_source_drift > max_source_drift_s:
            beat_warnings.append(f"high source drift: max={max_source_drift:.3f}s > {max_source_drift_s:.3f}s")
        if short_clip_count:
            beat_warnings.append(f"short_clip: {short_clip_count} placement(s) < {short_clip_threshold_s:.3f}s")
        if semantic_override and match_strategy != "chronological":
            beat_warnings.append("semantic overrode chronology")
        alternatives = []
        if visual_result:
            allowed = set(candidate_shot_ids.get(beat.beat_id, []))
            for (beat_id, shot_index), visual_score in visual_result.scores.items():
                if beat_id != beat.beat_id or (beat.beat_id in candidate_shot_ids and shot_index not in allowed):
                    continue
                shot = shots_by_index.get(shot_index)
                alternatives.append({
                    "shot_index": shot_index,
                    "visual_score": visual_score,
                    "visual_raw_cosine": visual_result.raw_cosines.get((beat_id, shot_index)),
                    "total_score_no_reuse": round(
                        score_shot(
                            shot,
                            0,
                            weights,
                            semantic_scores.get((beat_id, shot_index), 0.0),
                            visual_score,
                        ),
                        6,
                    ) if shot is not None else None,
                    "selected_keyframe": visual_result.selected_keyframes.get((beat_id, shot_index)),
                    "drift_tier": candidate_drift_tiers.get((beat_id, shot_index)),
                })
            alternatives.sort(key=lambda item: (item["total_score_no_reuse"] or 0.0, item["visual_score"]), reverse=True)
            alternatives = alternatives[:5]
        beat_reports.append({
            "beat_id": beat.beat_id,
            "narration_preview": narration_preview(beat.narration),
            "source_window": {"start": beat.src_tc_start, "end": beat.src_tc_end},
            "avg_semantic_score": round(avg_semantic, 6),
            "avg_source_drift_s": round(avg_source_drift, 3),
            "max_source_drift_s": round(max_source_drift, 3),
            "repeat_ratio": round(repeat_ratio, 6),
            "n_reused": n_reused,
            "unique_shots": unique_shots,
            "min_clip_s": round(min_clip_s, 3),
            "short_clip_count": short_clip_count,
            "empty_placements": not bool(beat_selected),
            "in_opening_guard": in_opening_guard,
            "story_section": {"id": intent.story_section_id, "type": intent.story_section_type} if intent else None,
            "visual_intent": intent.visual_intent if intent else None,
            "chronology_mode": intent.chronology_mode if intent else None,
            "visual_queries": visual_result.queries.get(beat.beat_id, []) if visual_result else [],
            "visual_query_weights": visual_result.query_weights.get(beat.beat_id, []) if visual_result else [],
            "visual_alternatives": alternatives,
            "candidate_window": {
                "start": candidate_info.get("window_start"),
                "end": candidate_info.get("window_end"),
                "widen_count": candidate_info.get("widen_count", 0),
            },
            "candidate_capacity_s": candidate_info.get("total_capacity_s", 0.0),
            "primary_candidate_capacity_s": candidate_info.get("primary_capacity_s", 0.0),
            "dark_candidate_capacity_s": candidate_info.get("dark_capacity_s", 0.0),
            "required_duration_s": candidate_info.get("required_duration_s", 0.0),
            "capacity_exhausted": bool(candidate_info.get("capacity_exhausted", False)),
            "dark_candidate_ids": sorted(dark_candidate_ids),
            "dark_selected_ids": candidate_info.get("dark_selected_ids", []),
            "unused_source_reuse_count": candidate_info.get("unused_source_reuse_count", 0),
            "overlapping_repeat_count": candidate_info.get("overlapping_repeat_count", 0),
            "content_anchor_used": bool(candidate_info.get("content_anchor_used", False)),
            "content_anchor_intervals": candidate_info.get("content_anchor_intervals", []),
            "content_anchor_interval_weights": candidate_info.get("content_anchor_interval_weights", []),
            "content_anchor_segment_ids": candidate_info.get("content_anchor_segment_ids", []),
            "content_anchor_threshold": candidate_info.get("content_anchor_threshold"),
            "content_anchor_capacity_s": candidate_info.get("content_anchor_capacity_s"),
            "opening_intra_beat_align_used": bool(candidate_info.get("opening_intra_beat_align_used", False)),
            "opening_intra_beat_chunks": candidate_info.get("opening_intra_beat_chunks", []),
            "opening_intra_beat_replaced_ranges": candidate_info.get("opening_intra_beat_replaced_ranges", []),
            "ordered_fill_used": ordered_fill_used,
            "chronology_mismatch": chronology_mismatch,
            "intent_match_score": 1.0 if section is not None else 0.0,
            "selected": selected,
            "warnings": beat_warnings,
        })
    excluded_intro = [shot.index for shot in shots if not shot.is_story]
    return {
        "version": 9,
        "match_strategy": match_strategy,
        "max_source_drift_s": max_source_drift_s,
        "semantic_enabled": bool(semantic_scores),
        "semantic_provider": semantic_result.provider if semantic_result else ("tfidf" if semantic_scores else "off"),
        "semantic_model": semantic_result.model if semantic_result else None,
        "semantic_device": semantic_result.device if semantic_result else None,
        "semantic_cache_hits": semantic_result.cache_hits if semantic_result else [],
        "visual_enabled": bool(visual_scores),
        "visual_provider": visual_result.provider if visual_result else "off",
        "visual_model": visual_result.model if visual_result else None,
        "visual_device": visual_result.device if visual_result else None,
        "visual_cache_hits": visual_result.cache_hits if visual_result else [],
        "visual_warnings": visual_result.warnings if visual_result else [],
        "min_semantic_score": min_semantic_score,
        "short_clip_threshold_s": short_clip_threshold_s,
        "n_intro_excluded": len(excluded_intro),
        "excluded_intro_candidates": excluded_intro[:200],
        "selected_from_non_story": any((not shots_by_index.get(item.shot_index, Shot(src="unknown", index=0, tc_start=0, tc_end=1, duration=1, thumb="unknown", motion_score=0, face_count=0, face_area=0, brightness=0, is_usable=False)).is_story) for item in placements),
        "beats": beat_reports,
    }

def source_drift(placement: EdlPlacement, expected_src_position: float) -> float:
    if placement.src_in <= expected_src_position <= placement.src_out:
        return 0.0
    return min(abs(placement.src_in - expected_src_position), abs(placement.src_out - expected_src_position))
