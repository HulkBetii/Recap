from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common.schema import EdlPlacement, ReviewBeat, ReviewIntent, write_json
from broll.schema import BrollCandidate, BrollPlan, read_json, stable_id

MOJIBAKE_MARKERS = ("?", "?", "?", "??", "??", "?", "?")


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


def ascii_only(text: str) -> str:
    return "".join(char if ord(char) < 128 else " " for char in text)


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


def _load_intent_map(path: Path | None) -> dict[int, str]:
    if path is None or not path.is_file():
        return {}
    try:
        intents = [ReviewIntent.model_validate(item) for item in read_json(path)]
    except Exception:
        return {}
    return {intent.beat_id: intent.visual_intent for intent in intents}


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
    if "high reuse" in warning_text or src_count > 1 or placement.reused:
        reasons.append("high_reuse")
        score += 4.0 + src_count
    if "source_order_mismatch" in warning_text or "source order" in warning_text or sync.get("source_order_mismatch") is True:
        reasons.append("source_order_mismatch")
        score += 3.0
    if "transition" in warning_text or duration_s <= 1.0:
        reasons.append("transition")
        score += 1.0
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
    return sorted(set(reasons)), score, warnings

INTENT_SCENE_MAP = {
    "character_intro": "a tense character-focused portrait in an urban crime drama",
    "dialogue": "two people in a tense conversation, cinematic police thriller mood",
    "location": "an atmospheric urban crime location, night market or back alley mood",
    "action": "a dynamic crime thriller action moment with urgent movement",
    "reaction": "a dramatic reaction shot showing fear, pressure, or realization",
    "reveal": "a suspenseful reveal moment with investigative clues and dramatic shadows",
    "transition": "an atmospheric transition image between crime investigation scenes",
    "ending": "a solemn ending image for a gritty police crime story",
}

REASON_SCENE_MAP = {
    "high_reuse": "use a fresh non-repetitive composition that can replace repeated footage",
    "source_order_mismatch": "make it feel like a neutral bridge that will not imply exact film chronology",
    "transition": "design it as a clean transition card without any written text",
    "ratio_fill": "make it visually rich enough to cover a narration-only filler moment",
    "long_clip": "include layered depth and subtle visual detail suitable for a longer hold",
    "high_source_drift": "keep the scene concept broad and story-safe rather than matching a precise action",
}


def _prompt_for(*, intent: str | None, reasons: list[str]) -> str:
    scene = INTENT_SCENE_MAP.get(intent or "", "a suspenseful moment in a gritty Vietnamese crime movie recap")
    reason_guidance = ", ".join(REASON_SCENE_MAP[reason] for reason in reasons if reason in REASON_SCENE_MAP)
    if not reason_guidance:
        reason_guidance = "create a clear cinematic image that supports narration without copying film footage"
    prompt = (
        "Cinematic 16:9 still image for a Vietnamese crime movie recap, realistic moody lighting, "
        "dramatic composition, high detail, shallow depth of field, no text, no logo, no watermark, "
        "no subtitles, no typography, no recognizable actor likeness, not a movie screenshot. "
        f"Scene idea: {scene}. Purpose: {reason_guidance}."
    )
    return ascii_only(prompt).replace("  ", " ").strip()

def build_broll_plan(
    *,
    edl_path: Path,
    qa_path: Path | None = None,
    sync_qa_path: Path | None = None,
    review_script_path: Path | None = None,
    review_intent_path: Path | None = None,
    output_plan_path: Path,
    output_prompts_path: Path,
    max_replacement_ratio: float = 0.30,
    max_broll_per_parent_beat: int = 1,
    exclude_opening_s: float = 5.5,
) -> BrollPlan:
    placements = [EdlPlacement.model_validate(item) for item in read_json(edl_path)]
    qa_map = _qa_by_beat(qa_path)
    sync_map = _sync_by_beat(sync_qa_path)
    review_map = _load_review_map(review_script_path)
    intent_map = _load_intent_map(review_intent_path)
    source_counts = Counter((p.src, round(p.src_in, 2), round(p.src_out, 2)) for p in placements)
    per_beat = defaultdict(int)
    candidates: list[BrollCandidate] = []
    for placement in placements:
        if placement.tl_start < exclude_opening_s:
            continue
        if per_beat[placement.beat_id] >= max_broll_per_parent_beat:
            continue
        qa = qa_map.get(placement.beat_id, {})
        sync = sync_map.get(placement.beat_id, {})
        reasons, score, warnings = _placement_reasons(placement, qa, sync, source_counts[(placement.src, round(placement.src_in, 2), round(placement.src_out, 2))])
        if not reasons:
            continue
        narration = review_map.get(placement.beat_id, "")
        asset_id = stable_id({"beat_id": placement.beat_id, "tl_start": round(placement.tl_start, 3), "src_in": round(placement.src_in, 3)})
        candidates.append(BrollCandidate(
            asset_id=asset_id,
            beat_id=placement.beat_id,
            shot_index=placement.shot_index,
            tl_start=round(placement.tl_start, 3),
            tl_end=round(placement.tl_end, 3),
            src=placement.src,
            src_in=round(placement.src_in, 3),
            src_out=round(placement.src_out, 3),
            duration_s=round(placement.tl_end - placement.tl_start, 3),
            narration_preview=_preview(narration),
            prompt=_prompt_for(intent=intent_map.get(placement.beat_id), reasons=reasons),
            reasons=reasons,
            warnings=warnings,
            rank_score=round(score, 3),
        ))
        per_beat[placement.beat_id] += 1
    candidates.sort(key=lambda item: (-item.rank_score, item.tl_start, item.beat_id))
    target = int(len(placements) * max_replacement_ratio)
    if max_replacement_ratio > 0 and target == 0 and candidates:
        target = 1
    selected = candidates[:target]
    plan = BrollPlan(
        source_edl=str(edl_path),
        max_replacement_ratio=max_replacement_ratio,
        max_broll_per_parent_beat=max_broll_per_parent_beat,
        exclude_opening_s=exclude_opening_s,
        n_placements=len(placements),
        n_candidates=len(selected),
        target_replacements=target,
        original_footage_ratio_estimate=round(1.0 - (len(selected) / len(placements) if placements else 0.0), 4),
        candidates=selected,
        warnings=[] if selected else ["no broll candidates selected"],
    )
    write_json(output_plan_path, plan)
    output_prompts_path.parent.mkdir(parents=True, exist_ok=True)
    with output_prompts_path.open("w", encoding="utf-8") as handle:
        for candidate in selected:
            handle.write(json.dumps({"asset_id": candidate.asset_id, "prompt": candidate.prompt, "suggested_filename": f"{candidate.asset_id}.png"}, ensure_ascii=False) + "\n")
    return plan

