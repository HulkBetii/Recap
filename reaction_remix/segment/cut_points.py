from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from common.schema import ReactionCutKind, ReactionCutSafetyMode, ReactionWord


INSUFFICIENT_SPEECH_PADDING_CONFIDENCE = 0.89
WORD_EDGE_CONFIDENCE = 0.90
BoundaryPolicy = Literal["strict", "strict-or-word-edge"]


@dataclass(frozen=True)
class SelectedCut:
    tc: float
    kind: ReactionCutKind
    confidence: float
    safety_mode: ReactionCutSafetyMode | None
    left_handle_s: float
    right_handle_s: float


def cuts_through_word(tc: float, words: list[ReactionWord]) -> bool:
    return any(word.tc_start + 1e-6 < tc < word.tc_end - 1e-6 for word in words)


def source_boundary_cut(tc: float) -> SelectedCut:
    return SelectedCut(
        tc=tc,
        kind="source_boundary",
        confidence=1.0,
        safety_mode="source_boundary",
        left_handle_s=0.0,
        right_handle_s=0.0,
    )


def _with_safety(
    tc: float,
    kind: ReactionCutKind,
    confidence: float,
    *,
    previous_content_end: float,
    next_content_start: float,
    words: list[ReactionWord],
    speech_padding_s: float,
    word_edge_eligible: bool,
) -> SelectedCut:
    left_handle_s = tc - previous_content_end
    right_handle_s = next_content_start - tc
    if abs(left_handle_s) <= 1e-6:
        left_handle_s = 0.0
    if abs(right_handle_s) <= 1e-6:
        right_handle_s = 0.0
    has_content_overlap = next_content_start < previous_content_end - 1e-6
    has_full_padding = (
        not has_content_overlap
        and left_handle_s >= speech_padding_s - 1e-6
        and right_handle_s >= speech_padding_s - 1e-6
    )
    clamped_confidence = max(0.0, min(1.0, confidence))
    if has_full_padding:
        safety_mode: ReactionCutSafetyMode = "full_handle"
    elif has_content_overlap:
        safety_mode = "overlap"
        clamped_confidence = min(clamped_confidence, INSUFFICIENT_SPEECH_PADDING_CONFIDENCE)
    elif (
        word_edge_eligible
        and clamped_confidence >= WORD_EDGE_CONFIDENCE
        and not cuts_through_word(tc, words)
    ):
        safety_mode = "word_edge"
        clamped_confidence = WORD_EDGE_CONFIDENCE
    else:
        # Protected reaction boundaries may use a clean word edge without
        # claiming the stricter narrator-only word_edge safety guarantee.
        safety_mode = None
        clamped_confidence = min(clamped_confidence, INSUFFICIENT_SPEECH_PADDING_CONFIDENCE)
    return SelectedCut(
        tc=tc,
        kind=kind,
        confidence=clamped_confidence,
        safety_mode=safety_mode,
        left_handle_s=left_handle_s,
        right_handle_s=right_handle_s,
    )


def select_cut(
    previous_content_end: float,
    next_content_start: float,
    *,
    scene_boundaries: list[float],
    words: list[ReactionWord],
    scene_window_s: float,
    min_silence_s: float,
    speech_padding_s: float,
    boundary_confidence: float,
    boundary_policy: BoundaryPolicy = "strict",
    word_edge_eligible: bool = False,
) -> SelectedCut:
    if boundary_policy not in {"strict", "strict-or-word-edge"}:
        raise ValueError("boundary_policy must be strict or strict-or-word-edge")
    signed_gap = next_content_start - previous_content_end
    gap = max(0.0, signed_gap)
    if gap >= max(min_silence_s, speech_padding_s * 2):
        return _with_safety(
            previous_content_end + gap / 2,
            "silence_midpoint",
            0.98,
            previous_content_end=previous_content_end,
            next_content_start=next_content_start,
            words=words,
            speech_padding_s=speech_padding_s,
            word_edge_eligible=boundary_policy == "strict-or-word-edge" and word_edge_eligible,
        )
    target = previous_content_end + signed_gap / 2
    padded_start = previous_content_end + speech_padding_s
    padded_end = next_content_start - speech_padding_s
    has_full_padding = padded_start <= padded_end + 1e-6
    safe_start = padded_start if has_full_padding else min(previous_content_end, next_content_start)
    safe_end = padded_end if has_full_padding else max(previous_content_end, next_content_start)
    nearby_scenes = [
        tc
        for tc in scene_boundaries
        if abs(tc - target) <= scene_window_s
        and safe_start - 1e-6 <= tc <= safe_end + 1e-6
        and not cuts_through_word(tc, words)
    ]
    if nearby_scenes:
        return _with_safety(
            min(nearby_scenes, key=lambda tc: abs(tc - target)),
            "scene_boundary",
            0.95,
            previous_content_end=previous_content_end,
            next_content_start=next_content_start,
            words=words,
            speech_padding_s=speech_padding_s,
            word_edge_eligible=boundary_policy == "strict-or-word-edge" and word_edge_eligible,
        )
    cut_time = target
    if cuts_through_word(cut_time, words):
        candidates = [safe_start, safe_end]
        safe = [tc for tc in candidates if not cuts_through_word(tc, words)]
        if not safe:
            raise ValueError("could not derive a cut outside word timestamps")
        cut_time = min(safe, key=lambda tc: abs(tc - target))
    cut_time = max(safe_start, min(safe_end, cut_time))
    return _with_safety(
        cut_time,
        "turn_boundary",
        boundary_confidence,
        previous_content_end=previous_content_end,
        next_content_start=next_content_start,
        words=words,
        speech_padding_s=speech_padding_s,
        word_edge_eligible=boundary_policy == "strict-or-word-edge" and word_edge_eligible,
    )
