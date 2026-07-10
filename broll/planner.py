from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common.schema import EdlPlacement, ReviewBeat, Shot, write_json
from broll.schema import BrollCandidate, BrollPlan, read_json, stable_id

MOJIBAKE_MARKERS = ("Ã", "Â", "á", "à", "º", "»", "�")


def repair_mojibake(text: str) -> str:
    """Repair common UTF-8-as-cp1252 mojibake without touching normal Vietnamese."""
    if not text or not any(marker in text for marker in MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except UnicodeError:
        return text
    original_markers = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    repaired_markers = sum(repaired.count(marker) for marker in MOJIBAKE_MARKERS)
    return repaired if repaired_markers < original_markers else text


def _preview(text: str, max_chars: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", repair_mojibake(text or "")).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _load_review_map(path: Path | None) -> dict[int, str]:
    if path is None or not path.is_file():
        return {}
    result: dict[int, str] = {}
    for item in read_json(path):
        try:
            beat = ReviewBeat.model_validate(item)
            result[beat.beat_id] = beat.narration
        except Exception:
            beat_id = item.get("beat_id") if isinstance(item, dict) else None
            narration = item.get("narration") if isinstance(item, dict) else None
            if isinstance(beat_id, int) and isinstance(narration, str):
                result[beat_id] = narration
    return result


def _load_shots(path: Path) -> list[Shot]:
    return [Shot.model_validate(item) for item in read_json(path)]


def _qa_by_beat(path: Path | None) -> dict[int, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = read_json(path)
    result: dict[int, dict[str, Any]] = {}
    for beat in payload.get("beats", []) if isinstance(payload, dict) else []:
        beat_id = beat.get("beat_id")
        if isinstance(beat_id, int):
            result[beat_id] = beat
    return result


def _sync_by_beat(path: Path | None) -> dict[int, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = read_json(path)
    result: dict[int, dict[str, Any]] = {}
    items = payload.get("beats", payload.get("mismatches", [])) if isinstance(payload, dict) else []
    for beat in items:
        beat_id = beat.get("beat_id")
        if isinstance(beat_id, int):
            result[beat_id] = beat
    return result


def _beat_warnings(qa: dict[str, Any], sync: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for source in (qa, sync):
        for item in source.get("warnings", []) if isinstance(source, dict) else []:
            if isinstance(item, str):
                warnings.append(item)
    return warnings


def _placement_reasons(placement: EdlPlacement, qa: dict[str, Any], sync: dict[str, Any], src_count: int) -> tuple[list[str], float, list[str]]:
    reasons: list[str] = []
    warnings = _beat_warnings(qa, sync)
    score = 0.0
    warning_text = " ".join(warnings).lower()
    duration_s = placement.tl_end - placement.tl_start
    if "high reuse" in warning_text or "high repeat" in warning_text or src_count > 1 or placement.reused:
        reasons.append("high_reuse")
        score += 4.0 + src_count
    if "source_order_mismatch" in warning_text or "source order" in warning_text or sync.get("source_order_mismatch") is True:
        reasons.append("source_order_mismatch")
        score += 3.0
    if "transition" in warning_text:
        reasons.append("transition")
    if "ratio fill" in warning_text or "duration fill" in warning_text or "long clip" in warning_text or duration_s > 4.8:
        reasons.append("ratio_fill")
        score += 1.25
    if "long clip" in warning_text or duration_s > 4.8:
        reasons.append("long_clip")
        score += 1.5
    drift = 0.0
    selected = qa.get("selected", []) if isinstance(qa, dict) else []
    for item in selected:
        if item.get("shot_index") == placement.shot_index and abs(float(item.get("tl_start", -999)) - placement.tl_start) < 0.05:
            drift = max(drift, float(item.get("source_drift_s") or 0.0))
    if drift >= 12.0:
        reasons.append("high_source_drift")
        score += min(3.0, drift / 10.0)
    if not reasons and placement.reused:
        reasons.append("reused")
        score += 1.0
    core_reasons = {"high_reuse", "source_order_mismatch", "ratio_fill", "long_clip", "high_source_drift", "reused"}
    if duration_s < 0.5 or not (core_reasons & set(reasons)):
        return sorted(set(reasons)), 0.0, warnings
    return sorted(set(reasons)), score, warnings


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _shot_midpoint(shot: Shot) -> float:
    return shot.tc_start + (shot.tc_end - shot.tc_start) / 2.0


def _score_frame_shots(
    placement: EdlPlacement,
    shots: list[Shot],
    *,
    min_distance: int,
    recent_frame_shots: set[int],
) -> list[tuple[float, Shot, str, int]]:
    source_mid = placement.src_in + max(0.0, placement.src_out - placement.src_in) / 2.0
    window_start = max(0.0, placement.src_in - 12.0)
    window_end = placement.src_out + 12.0
    scored: list[tuple[float, Shot, str, int]] = []
    for shot in shots:
        if shot.index == placement.shot_index:
            continue
        shot_distance = abs(shot.index - placement.shot_index)
        if shot_distance < min_distance:
            continue
        if shot.index in recent_frame_shots:
            continue
        if not shot.is_story or not shot.is_usable:
            continue
        if shot.brightness < 0.08:
            continue
        overlap = _overlap(shot.tc_start, shot.tc_end, window_start, window_end)
        distance = abs(_shot_midpoint(shot) - source_mid)
        if overlap <= 0 and distance > 30.0:
            continue
        proximity = max(0.0, 1.0 - min(distance, 30.0) / 30.0)
        overlap_bonus = min(1.0, overlap / max(0.1, shot.duration))
        quality = 0.35 * shot.motion_score + 0.25 * shot.brightness + 0.20 * min(1.0, shot.face_count / 2.0) + 0.20 * min(1.0, shot.face_area * 4.0)
        score = (2.0 * overlap_bonus) + (1.5 * proximity) + quality + min(1.0, shot_distance / 10.0) * 0.15
        reason = "within_source_window" if overlap > 0 else "near_source_window"
        scored.append((score, shot, reason, min_distance))
    scored.sort(key=lambda item: (item[0], item[1].motion_score, item[1].brightness, abs(item[1].index - placement.shot_index)), reverse=True)
    return scored


def _select_frame_shot(
    placement: EdlPlacement,
    shots: list[Shot],
    *,
    min_frame_shot_distance: int,
    recent_frame_shots: set[int] | None = None,
) -> tuple[Shot | None, str, int]:
    recent_frame_shots = recent_frame_shots or set()
    start_distance = max(1, min_frame_shot_distance)
    for distance_threshold in range(start_distance, 0, -1):
        scored = _score_frame_shots(
            placement,
            shots,
            min_distance=distance_threshold,
            recent_frame_shots=recent_frame_shots,
        )
        if scored:
            best = scored[0]
            reason = best[2]
            if distance_threshold < start_distance:
                reason = f"{reason}_relaxed_distance_{distance_threshold}"
            return best[1], reason, distance_threshold
    return None, "no_alternative_frame_shot", 0


def build_broll_plan(
    *,
    edl_path: Path,
    shots_path: Path,
    qa_path: Path | None = None,
    sync_qa_path: Path | None = None,
    review_script_path: Path | None = None,
    review_intent_path: Path | None = None,
    output_plan_path: Path,
    max_replacement_ratio: float = 0.30,
    max_broll_per_parent_beat: int = 1,
    exclude_opening_s: float = 5.5,
    min_broll_duration_s: float = 1.0,
    min_frame_shot_distance: int = 3,
    frame_reuse_window_s: float = 20.0,
) -> BrollPlan:
    placements = [EdlPlacement.model_validate(item) for item in read_json(edl_path)]
    shots = _load_shots(shots_path)
    review_map = _load_review_map(review_script_path)
    qa_by_beat = _qa_by_beat(qa_path)
    sync_by_beat = _sync_by_beat(sync_qa_path)
    src_counter = Counter((placement.beat_id, placement.shot_index) for placement in placements)
    raw_candidates: list[tuple[float, BrollCandidate]] = []
    warnings: list[str] = []
    n_skipped_short_duration = 0
    n_frame_keep_original_no_alternative = 0
    recent_frame_choices: list[tuple[float, int]] = []
    for placement in placements:
        if placement.tl_start < exclude_opening_s:
            continue
        duration_s = placement.tl_end - placement.tl_start
        if duration_s < min_broll_duration_s:
            n_skipped_short_duration += 1
            continue
        qa = qa_by_beat.get(placement.beat_id, {})
        sync = sync_by_beat.get(placement.beat_id, {})
        reasons, score, beat_warnings = _placement_reasons(placement, qa, sync, src_counter[(placement.beat_id, placement.shot_index)])
        if score <= 0:
            continue
        recent_frame_shots = {
            frame_shot_index
            for tl_start, frame_shot_index in recent_frame_choices
            if frame_reuse_window_s > 0 and abs(placement.tl_start - tl_start) <= frame_reuse_window_s
        }
        frame_shot, frame_reason, distance_used = _select_frame_shot(
            placement,
            shots,
            min_frame_shot_distance=min_frame_shot_distance,
            recent_frame_shots=recent_frame_shots,
        )
        if frame_shot is None:
            n_frame_keep_original_no_alternative += 1
            warnings.append(f"no alternative frame shot for beat={placement.beat_id} tl={placement.tl_start:.3f}")
            continue
        recent_frame_choices.append((placement.tl_start, frame_shot.index))
        frame_tc = _shot_midpoint(frame_shot)
        frame_tc = min(max(frame_tc, frame_shot.tc_start), max(frame_shot.tc_start, frame_shot.tc_end - 0.001))
        frame_id = stable_id({"beat_id": placement.beat_id, "tl_start": round(placement.tl_start, 3), "frame_shot_index": frame_shot.index}, prefix="bf")
        candidate = BrollCandidate(
            frame_id=frame_id,
            beat_id=placement.beat_id,
            shot_index=placement.shot_index,
            tl_start=placement.tl_start,
            tl_end=placement.tl_end,
            src=placement.src,
            src_in=placement.src_in,
            src_out=placement.src_out,
            duration_s=round(duration_s, 6),
            narration_preview=_preview(review_map.get(placement.beat_id, "")),
            reasons=reasons,
            warnings=beat_warnings,
            rank_score=round(score, 4),
            frame_src=frame_shot.src,
            frame_tc=round(frame_tc, 6),
            frame_shot_index=frame_shot.index,
            frame_shot_distance_used=distance_used,
            frame_reason=frame_reason,
        )
        raw_candidates.append((score, candidate))
    raw_candidates.sort(key=lambda item: item[0], reverse=True)
    target = min(len(raw_candidates), max(0, int(len(placements) * max_replacement_ratio)))
    selected: list[BrollCandidate] = []
    per_beat: defaultdict[int, int] = defaultdict(int)
    for _, candidate in raw_candidates:
        if len(selected) >= target:
            break
        if per_beat[candidate.beat_id] >= max_broll_per_parent_beat:
            continue
        selected.append(candidate)
        per_beat[candidate.beat_id] += 1
    if not selected:
        warnings.append("no broll candidates selected")
    if n_skipped_short_duration:
        warnings.append(f"skipped {n_skipped_short_duration} broll candidate(s) shorter than {min_broll_duration_s:.3f}s")
    plan = BrollPlan(
        source_edl=str(edl_path),
        source_shots=str(shots_path),
        max_replacement_ratio=max_replacement_ratio,
        max_broll_per_parent_beat=max_broll_per_parent_beat,
        exclude_opening_s=exclude_opening_s,
        min_broll_duration_s=min_broll_duration_s,
        min_frame_shot_distance=min_frame_shot_distance,
        frame_reuse_window_s=frame_reuse_window_s,
        n_skipped_short_duration=n_skipped_short_duration,
        n_frame_keep_original_no_alternative=n_frame_keep_original_no_alternative,
        n_placements=len(placements),
        n_candidates=len(selected),
        target_replacements=target,
        original_footage_ratio_estimate=round(1.0 - (len(selected) / len(placements) if placements else 0.0), 4),
        candidates=selected,
        warnings=warnings,
    )
    write_json(output_plan_path, plan)
    return plan
