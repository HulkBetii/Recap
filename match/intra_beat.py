from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.candidates import is_dark_fallback_candidate
from match.fill import source_position_for_progress


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u2026])\s+")
ENTITY_TOKEN_RE = re.compile(r"[\w\u00c0-\u1ef9]+(?:-[\w\u00c0-\u1ef9]+)*", re.UNICODE)
ENTITY_STOPWORDS = {"anh", "cô", "gã", "người", "trước", "trên", "đúng", "ngay", "sau"}
MIN_AUDIO_DURATION_S = 45.0
MIN_SOURCE_AUDIO_RATIO = 3.0
LONG_BEAT_MIN_SOURCE_AUDIO_RATIO = 2.5
LONG_BEAT_MIN_DRIFT_S = 18.0
LONG_BEAT_DRIFT_MULTIPLIER = 1.5
ANALYSIS_DURATION_S = 30.0
MIN_SENTENCE_COUNT = 4
MIN_ANCHOR_SCORE = 0.50
FLEXIBLE_MIN_ANCHOR_SCORE = 0.54
MIN_BASELINE_SHIFT_S = 6.0
CHRONOLOGY_PRIOR_WEIGHT = 0.10
FLEXIBLE_CHRONOLOGY_PRIOR_WEIGHT = 0.03
FLEXIBLE_MAX_SOURCE_JUMP_S = 45.0
FLEXIBLE_SOURCE_JUMP_PENALTY_WEIGHT = 0.18
FLEXIBLE_SOURCE_JUMP_OVER_CAP_PENALTY = 0.12
FLEXIBLE_SOURCE_JUMP_OVER_CAP_WEIGHT = 0.06
FLEXIBLE_BASELINE_DRIFT_PENALTY_WEIGHT = 0.16
FLEXIBLE_BASELINE_DRIFT_WINDOW_S = 90.0
FLEXIBLE_MIN_DRIFT_IMPROVEMENT_S = 0.5
SHORT_SENTENCE_S = 3.0
MAX_CHUNK_DURATION_S = 45.0
ENTITY_ANCHOR_SCORE_TOLERANCE = 0.12


@dataclass(frozen=True)
class SentenceTiming:
    beat_id: int
    sentence_index: int
    text: str
    tl_start: float
    tl_end: float

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


@dataclass(frozen=True)
class AlignmentChunk:
    beat_id: int
    sentence_indices: tuple[int, ...]
    text: str
    tl_start: float
    tl_end: float
    anchor_shot_index: int
    anchor_source_s: float
    semantic_score: float
    baseline_source_s: float

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


@dataclass
class IntraBeatAlignmentResult:
    placements: list[EdlPlacement]
    diagnostics: list[dict[str, object]]
    replaced_ranges: list[tuple[float, float]]
    warnings: list[str]
    accepted: bool = False
    rejected_reason: str | None = None
    baseline_max_drift_s: float | None = None
    refined_max_drift_s: float | None = None
    baseline_warning_count: int | None = None
    refined_warning_count: int | None = None
    max_source_jump_s: float = 0.0

    @property
    def used(self) -> bool:
        return bool(self.replaced_ranges)


@dataclass
class HookLeadingGuardResult:
    placements: list[EdlPlacement]
    used: bool
    original_shot_index: int | None
    replacement_shot_ids: list[int]
    warnings: list[str]


def split_narration_sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(normalized) if part.strip()]


def estimate_sentence_timings(beat: ReviewBeat, timing: BeatTiming) -> list[SentenceTiming]:
    sentences = split_narration_sentences(beat.narration)
    if not sentences:
        return []
    weights = [max(1, sum(character.isalnum() for character in sentence)) for sentence in sentences]
    total_weight = sum(weights)
    output: list[SentenceTiming] = []
    cursor = timing.tl_start
    consumed_weight = 0
    for index, (sentence, weight) in enumerate(zip(sentences, weights)):
        consumed_weight += weight
        tl_end = timing.tl_end if index == len(sentences) - 1 else round(
            timing.tl_start + timing.duration * consumed_weight / total_weight,
            3,
        )
        output.append(
            SentenceTiming(
                beat_id=beat.beat_id,
                sentence_index=index,
                text=sentence,
                tl_start=round(cursor, 3),
                tl_end=round(tl_end, 3),
            )
        )
        cursor = tl_end
    return output


def prepare_opening_alignment_sentences(
    *,
    beats: list[ReviewBeat],
    timings: list[BeatTiming],
    enabled: bool,
    semantic_mode: str,
    strict_timecodes: bool,
    opening_guard_s: float,
) -> dict[int, list[SentenceTiming]]:
    if not enabled or semantic_mode != "bge-m3" or not strict_timecodes or opening_guard_s <= 0:
        return {}
    beats_by_id = {beat.beat_id: beat for beat in beats}
    for timing in sorted(timings, key=lambda item: item.tl_start):
        beat = beats_by_id[timing.beat_id]
        if beat.is_hook or timing.tl_start >= opening_guard_s or timing.duration < MIN_AUDIO_DURATION_S:
            continue
        source_audio_ratio = (beat.src_tc_end - beat.src_tc_start) / timing.duration
        if source_audio_ratio < MIN_SOURCE_AUDIO_RATIO:
            continue
        sentence_timings = estimate_sentence_timings(beat, timing)
        if len(sentence_timings) < MIN_SENTENCE_COUNT:
            continue
        analysis_end = min(timing.tl_end, timing.tl_start + ANALYSIS_DURATION_S)
        analyzed = [sentence for sentence in sentence_timings if sentence.tl_end <= analysis_end + 1e-6]
        if len(analyzed) >= MIN_SENTENCE_COUNT:
            return {beat.beat_id: analyzed}
    return {}


def prepare_intra_beat_alignment_sentences(
    *,
    beats: list[ReviewBeat],
    timings: list[BeatTiming],
    enabled: bool,
    sentence_refinement_enabled: bool = False,
    semantic_mode: str,
    strict_timecodes: bool,
    opening_guard_s: float,
) -> dict[int, list[SentenceTiming]]:
    if (not enabled and not sentence_refinement_enabled) or semantic_mode != "bge-m3" or not strict_timecodes:
        return {}
    beats_by_id = {beat.beat_id: beat for beat in beats}
    output: dict[int, list[SentenceTiming]] = {}
    opening_selected = False
    for timing in sorted(timings, key=lambda item: item.tl_start):
        beat = beats_by_id[timing.beat_id]
        if beat.is_hook or timing.duration < MIN_AUDIO_DURATION_S:
            continue
        source_audio_ratio = (beat.src_tc_end - beat.src_tc_start) / timing.duration
        sentence_timings = estimate_sentence_timings(beat, timing)
        if len(sentence_timings) < MIN_SENTENCE_COUNT:
            continue
        in_opening_guard = opening_guard_s > 0 and timing.tl_start < opening_guard_s
        if in_opening_guard and enabled and not opening_selected and source_audio_ratio >= MIN_SOURCE_AUDIO_RATIO:
            analysis_end = min(timing.tl_end, timing.tl_start + ANALYSIS_DURATION_S)
            analyzed = [sentence for sentence in sentence_timings if sentence.tl_end <= analysis_end + 1e-6]
            if len(analyzed) >= MIN_SENTENCE_COUNT:
                output[beat.beat_id] = analyzed
                opening_selected = True
            continue
        if (
            not in_opening_guard
            and (enabled or sentence_refinement_enabled)
            and source_audio_ratio >= LONG_BEAT_MIN_SOURCE_AUDIO_RATIO
        ):
            output[beat.beat_id] = sentence_timings
    return output


def alignment_queries(sentences_by_beat: dict[int, list[SentenceTiming]]) -> dict[tuple[int, int], str]:
    return {
        (beat_id, sentence.sentence_index): sentence.text
        for beat_id, sentences in sentences_by_beat.items()
        for sentence in sentences
    }


def select_monotonic_anchors(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    sentences: list[SentenceTiming],
    shots: list[Shot],
    query_shot_scores: dict[tuple[int, int, int], float],
    chronology_prior_weight: float = CHRONOLOGY_PRIOR_WEIGHT,
    allow_dark_fallback: bool = False,
    min_visual_clip: float = 0.6,
) -> list[AlignmentChunk]:
    candidates = sorted(
        [
            shot
            for shot in shots
            if shot.is_story
            and (shot.is_usable or (allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)))
            and shot.tc_start < beat.src_tc_end
            and beat.src_tc_start < shot.tc_end
        ],
        key=lambda shot: (shot.tc_start, shot.index),
    )
    if not sentences or not candidates:
        return []
    source_span = beat.src_tc_end - beat.src_tc_start
    scores: list[list[float]] = []
    semantic_values: list[list[float]] = []
    baseline_positions: list[float] = []
    for sentence in sentences:
        midpoint = (sentence.tl_start + sentence.tl_end) / 2
        progress = min(1.0, max(0.0, (midpoint - timing.tl_start) / timing.duration))
        baseline_source = beat.src_tc_start + source_span * progress
        baseline_positions.append(baseline_source)
        semantic_row: list[float] = []
        score_row: list[float] = []
        for shot in candidates:
            semantic_score = query_shot_scores.get((beat.beat_id, sentence.sentence_index, shot.index), 0.0)
            shot_midpoint = (shot.tc_start + shot.tc_end) / 2
            chronology_prior = max(0.0, 1.0 - abs(shot_midpoint - baseline_source) / max(source_span, 1e-6))
            baseline_drift_penalty = flexible_baseline_drift_penalty(abs(shot_midpoint - baseline_source), source_span)
            semantic_row.append(semantic_score)
            score_row.append(semantic_score + chronology_prior_weight * chronology_prior - baseline_drift_penalty)
        semantic_values.append(semantic_row)
        scores.append(score_row)

    dp = [[float("-inf")] * len(candidates) for _ in sentences]
    previous = [[-1] * len(candidates) for _ in sentences]
    for shot_index, score in enumerate(scores[0]):
        dp[0][shot_index] = score
    for sentence_index in range(1, len(sentences)):
        best_value = float("-inf")
        best_index = -1
        for shot_index in range(len(candidates)):
            if dp[sentence_index - 1][shot_index] > best_value:
                best_value = dp[sentence_index - 1][shot_index]
                best_index = shot_index
            dp[sentence_index][shot_index] = best_value + scores[sentence_index][shot_index]
            previous[sentence_index][shot_index] = best_index
    final_index = max(range(len(candidates)), key=lambda index: (dp[-1][index], -index))
    selected_indices = [final_index]
    for sentence_index in range(len(sentences) - 1, 0, -1):
        selected_indices.append(previous[sentence_index][selected_indices[-1]])
    selected_indices.reverse()
    for sentence_index in range(len(sentences) - 1):
        current_index = selected_indices[sentence_index]
        next_index = selected_indices[sentence_index + 1]
        if not shared_entity_tokens(sentences[sentence_index].text, sentences[sentence_index + 1].text):
            continue
        if next_index < current_index or next_index - current_index > 2:
            continue
        current_score = semantic_values[sentence_index][current_index]
        shared_anchor_score = semantic_values[sentence_index][next_index]
        if shared_anchor_score + ENTITY_ANCHOR_SCORE_TOLERANCE >= current_score:
            selected_indices[sentence_index] = next_index

    chunks = [
        AlignmentChunk(
            beat_id=beat.beat_id,
            sentence_indices=(sentence.sentence_index,),
            text=sentence.text,
            tl_start=sentence.tl_start,
            tl_end=sentence.tl_end,
            anchor_shot_index=candidates[selected_index].index,
            anchor_source_s=(candidates[selected_index].tc_start + candidates[selected_index].tc_end) / 2,
            semantic_score=semantic_values[sentence_index][selected_index],
            baseline_source_s=baseline_positions[sentence_index],
        )
        for sentence_index, (sentence, selected_index) in enumerate(zip(sentences, selected_indices))
    ]
    return coalesce_alignment_chunks(chunks)

def select_flexible_anchors(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    sentences: list[SentenceTiming],
    shots: list[Shot],
    query_shot_scores: dict[tuple[int, int, int], float],
    chronology_prior_weight: float = FLEXIBLE_CHRONOLOGY_PRIOR_WEIGHT,
    allow_dark_fallback: bool = False,
    min_visual_clip: float = 0.6,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> list[AlignmentChunk]:
    candidates = sorted(
        [
            shot
            for shot in shots
            if shot.is_story
            and (shot.is_usable or (allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)))
            and shot.tc_start < beat.src_tc_end
            and beat.src_tc_start < shot.tc_end
        ],
        key=lambda shot: (shot.tc_start, shot.index),
    )
    if not sentences or not candidates:
        return []
    source_span = beat.src_tc_end - beat.src_tc_start
    source_midpoints = [(shot.tc_start + shot.tc_end) / 2 for shot in candidates]
    scores: list[list[float]] = []
    semantic_values: list[list[float]] = []
    baseline_positions: list[float] = []
    for sentence in sentences:
        midpoint = (sentence.tl_start + sentence.tl_end) / 2
        progress = min(1.0, max(0.0, (midpoint - timing.tl_start) / timing.duration))
        baseline_source = flexible_expected_source_position(
            beat=beat,
            progress=progress,
            source_intervals=source_intervals,
            source_interval_weights=source_interval_weights,
        )
        baseline_positions.append(baseline_source)

        semantic_row: list[float] = []
        score_row: list[float] = []
        for shot in candidates:
            semantic_score = query_shot_scores.get((beat.beat_id, sentence.sentence_index, shot.index), 0.0)
            shot_midpoint = (shot.tc_start + shot.tc_end) / 2
            chronology_prior = max(0.0, 1.0 - abs(shot_midpoint - baseline_source) / max(source_span, 1e-6))
            semantic_row.append(semantic_score)
            score_row.append(semantic_score + chronology_prior_weight * chronology_prior)
        semantic_values.append(semantic_row)
        scores.append(score_row)

    dp = [[float("-inf")] * len(candidates) for _ in sentences]
    previous = [[-1] * len(candidates) for _ in sentences]
    for shot_index, score in enumerate(scores[0]):
        dp[0][shot_index] = score
    for sentence_index in range(1, len(sentences)):
        expected_source_jump = abs(baseline_positions[sentence_index] - baseline_positions[sentence_index - 1])
        for shot_index, score in enumerate(scores[sentence_index]):
            current_source = source_midpoints[shot_index]
            best_value = float("-inf")
            best_index = -1
            best_jump = float("inf")
            for previous_index, previous_value in enumerate(dp[sentence_index - 1]):
                source_jump = abs(current_source - source_midpoints[previous_index])
                value = previous_value + score - flexible_source_jump_penalty(
                    source_jump,
                    expected_source_jump_s=expected_source_jump,
                )
                if (
                    value > best_value + 1e-12
                    or (
                        abs(value - best_value) <= 1e-12
                        and (
                            source_jump < best_jump - 1e-9
                            or (
                                abs(source_jump - best_jump) <= 1e-9
                                and previous_index < best_index
                            )
                        )
                    )
                ):
                    best_value = value
                    best_index = previous_index
                    best_jump = source_jump
            dp[sentence_index][shot_index] = best_value
            previous[sentence_index][shot_index] = best_index
    final_index = max(
        range(len(candidates)),
        key=lambda index: (
            dp[-1][index],
            scores[-1][index],
            -abs(source_midpoints[index] - baseline_positions[-1]),
            -index,
        ),
    )
    selected_indices = [final_index]
    for sentence_index in range(len(sentences) - 1, 0, -1):
        selected_indices.append(previous[sentence_index][selected_indices[-1]])
    selected_indices.reverse()

    chunks = [
        AlignmentChunk(
            beat_id=beat.beat_id,
            sentence_indices=(sentence.sentence_index,),
            text=sentence.text,
            tl_start=sentence.tl_start,
            tl_end=sentence.tl_end,
            anchor_shot_index=candidates[selected_index].index,
            anchor_source_s=source_midpoints[selected_index],
            semantic_score=semantic_values[sentence_index][selected_index],
            baseline_source_s=baseline_positions[sentence_index],
        )
        for sentence_index, (sentence, selected_index) in enumerate(zip(sentences, selected_indices))
    ]
    return coalesce_alignment_chunks(chunks)


def flexible_source_jump_penalty(source_jump_s: float, *, expected_source_jump_s: float = 0.0) -> float:
    unexpected_jump_s = max(0.0, source_jump_s - max(0.0, expected_source_jump_s))
    normalized_jump = min(1.0, unexpected_jump_s / max(FLEXIBLE_MAX_SOURCE_JUMP_S, 1e-6))
    penalty = FLEXIBLE_SOURCE_JUMP_PENALTY_WEIGHT * normalized_jump
    if unexpected_jump_s > FLEXIBLE_MAX_SOURCE_JUMP_S:
        over_cap_ratio = min(
            2.0,
            (unexpected_jump_s - FLEXIBLE_MAX_SOURCE_JUMP_S) / max(FLEXIBLE_MAX_SOURCE_JUMP_S, 1e-6),
        )
        penalty += FLEXIBLE_SOURCE_JUMP_OVER_CAP_PENALTY
        penalty += FLEXIBLE_SOURCE_JUMP_OVER_CAP_WEIGHT * over_cap_ratio
    return penalty


def flexible_baseline_drift_penalty(baseline_shift_s: float, source_span_s: float) -> float:
    window_s = max(FLEXIBLE_BASELINE_DRIFT_WINDOW_S, source_span_s * 0.25, 1e-6)
    return FLEXIBLE_BASELINE_DRIFT_PENALTY_WEIGHT * min(1.0, baseline_shift_s / window_s)


def flexible_expected_source_position(
    *,
    beat: ReviewBeat,
    progress: float,
    source_intervals: list[tuple[float, float]] | None,
    source_interval_weights: list[float] | None,
) -> float:
    if source_intervals:
        return source_position_for_progress(source_intervals, progress, weights=source_interval_weights)
    return beat.src_tc_start + (beat.src_tc_end - beat.src_tc_start) * progress


def shared_entity_tokens(left: str, right: str) -> set[str]:
    def entities(text: str) -> set[str]:
        output: set[str] = set()
        for token in ENTITY_TOKEN_RE.findall(text):
            lowered = token.lower()
            if len(lowered) < 3 or lowered in ENTITY_STOPWORDS:
                continue
            if "-" in token or token[0].isupper():
                output.add(lowered)
        return output

    return entities(left) & entities(right)


def coalesce_alignment_chunks(chunks: list[AlignmentChunk]) -> list[AlignmentChunk]:
    output: list[AlignmentChunk] = []
    for chunk in chunks:
        if (
            output
            and output[-1].anchor_shot_index == chunk.anchor_shot_index
            and chunk.tl_end - output[-1].tl_start <= MAX_CHUNK_DURATION_S + 1e-6
        ):
            output[-1] = merge_chunks(output[-1], chunk, anchor_from_right=True)
        else:
            output.append(chunk)

    index = 0
    while index < len(output):
        if output[index].duration >= SHORT_SENTENCE_S or len(output) == 1:
            index += 1
            continue
        candidates: list[tuple[float, int, bool]] = []
        if index > 0 and output[index - 1].duration + output[index].duration <= MAX_CHUNK_DURATION_S + 1e-6:
            candidates.append((abs(output[index - 1].anchor_source_s - output[index].anchor_source_s), index - 1, False))
        if index + 1 < len(output) and output[index].duration + output[index + 1].duration <= MAX_CHUNK_DURATION_S + 1e-6:
            candidates.append((abs(output[index + 1].anchor_source_s - output[index].anchor_source_s), index + 1, True))
        if not candidates:
            index += 1
            continue
        _distance, target_index, merge_forward = min(candidates, key=lambda item: (item[0], not item[2]))
        if merge_forward:
            output[target_index] = merge_chunks(output[index], output[target_index], anchor_from_right=True)
            del output[index]
        else:
            output[target_index] = merge_chunks(output[target_index], output[index], anchor_from_right=False)
            del output[index]
            index = max(0, index - 1)
    return output


def merge_chunks(left: AlignmentChunk, right: AlignmentChunk, *, anchor_from_right: bool) -> AlignmentChunk:
    anchor = right if anchor_from_right else left
    total_duration = max(left.duration + right.duration, 1e-6)
    semantic_score = (left.semantic_score * left.duration + right.semantic_score * right.duration) / total_duration
    baseline_source = (left.baseline_source_s * left.duration + right.baseline_source_s * right.duration) / total_duration
    return AlignmentChunk(
        beat_id=left.beat_id,
        sentence_indices=left.sentence_indices + right.sentence_indices,
        text=f"{left.text} {right.text}",
        tl_start=left.tl_start,
        tl_end=right.tl_end,
        anchor_shot_index=anchor.anchor_shot_index,
        anchor_source_s=anchor.anchor_source_s,
        semantic_score=semantic_score,
        baseline_source_s=baseline_source,
    )


def apply_intra_beat_alignment(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    baseline_placements: list[EdlPlacement],
    sentences: list[SentenceTiming],
    shots: list[Shot],
    query_shot_scores: dict[tuple[int, int, int], float],
    reuse_counts_before: dict[int, int],
    max_clip: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
    mode: str = "opening",
    max_source_drift_s: float = 12.0,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> IntraBeatAlignmentResult:
    flexible_mode = mode == "content_anchor_long_beat"
    if flexible_mode:
        chunks = select_flexible_anchors(
            beat=beat,
            timing=timing,
            sentences=sentences,
            shots=shots,
            query_shot_scores=query_shot_scores,
            allow_dark_fallback=allow_dark_fallback,
            min_visual_clip=min_visual_clip,
            source_intervals=source_intervals,
            source_interval_weights=source_interval_weights,
        )
    else:
        chunks = select_monotonic_anchors(
            beat=beat,
            timing=timing,
            sentences=sentences,
            shots=shots,
            query_shot_scores=query_shot_scores,
            allow_dark_fallback=allow_dark_fallback,
            min_visual_clip=min_visual_clip,
        )
    if mode == "long_beat":
        chunks = merge_low_confidence_transitions(beat, chunks)
    shots_by_index = {shot.index: shot for shot in shots}
    diagnostics: list[dict[str, object]] = []
    replacements: list[EdlPlacement] = []
    replaced_ranges: list[tuple[float, float]] = []
    warnings: list[str] = []
    source_audio_ratio = (beat.src_tc_end - beat.src_tc_start) / timing.duration
    planned_windows = plan_long_beat_source_windows(beat, chunks, shots_by_index) if mode == "long_beat" else {}
    previous_selected_source_midpoint: float | None = None
    previous_selected_expected_source: float | None = None

    for index, chunk in enumerate(chunks):
        anchor = shots_by_index.get(chunk.anchor_shot_index)
        next_anchor = shots_by_index.get(chunks[index + 1].anchor_shot_index) if index + 1 < len(chunks) else None
        replacement_tl_end = chunk.tl_end
        matching_tail = None if mode == "long_beat" else next(
            (
                placement
                for placement in baseline_placements
                if placement.shot_index == chunk.anchor_shot_index
                and placement.tl_start > chunk.tl_start + min_visual_clip
                and placement.tl_start < chunk.tl_end - 1e-6
                and placement.tl_end >= chunk.tl_end - 1e-6
            ),
            None,
        )
        if matching_tail is not None:
            replacement_tl_end = matching_tail.tl_start
        replacement_duration = replacement_tl_end - chunk.tl_start
        if index in planned_windows:
            window_start, window_end = planned_windows[index]
        elif matching_tail is not None:
            window_end = matching_tail.src_in
            window_start = max(beat.src_tc_start, window_end - replacement_duration)
        elif flexible_mode and anchor is not None:
            window_start, window_end = plan_flexible_source_window(
                beat=beat,
                anchor=anchor,
                replacement_duration=replacement_duration,
                shots=shots,
                max_clip=max_clip,
                min_visual_clip=min_visual_clip,
                allow_dark_fallback=allow_dark_fallback,
            )
        else:
            window_start = anchor.tc_start if anchor is not None else beat.src_tc_start
            if next_anchor is not None and next_anchor.tc_start > window_start:
                window_end = next_anchor.tc_start
            elif next_anchor is None:
                window_end = beat.src_tc_end
                window_start = max(
                    beat.src_tc_start,
                    min(window_start, window_end - max(max_clip, replacement_duration)),
                )
            else:
                window_end = min(beat.src_tc_end, window_start + max(max_clip, replacement_duration * source_audio_ratio))
        chronology_prior = max(
            0.0,
            1.0 - abs(chunk.anchor_source_s - chunk.baseline_source_s) / max(beat.src_tc_end - beat.src_tc_start, 1e-6),
        )
        anchor_score = chunk.semantic_score + CHRONOLOGY_PRIOR_WEIGHT * chronology_prior
        diagnostic: dict[str, object] = {
            "selection_mode": mode,
            "sentence_indices": list(chunk.sentence_indices),
            "text": chunk.text,
            "tl_start": round(chunk.tl_start, 3),
            "tl_end": round(chunk.tl_end, 3),
            "replacement_range": [round(chunk.tl_start, 3), round(replacement_tl_end, 3)],
            "anchor_shot_index": chunk.anchor_shot_index,
            "anchor_source_s": round(chunk.anchor_source_s, 3),
            "semantic_score": round(chunk.semantic_score, 6),
            "anchor_score": round(anchor_score, 6),
            "baseline_source_s": round(chunk.baseline_source_s, 3),
            "baseline_shift_s": round(abs(chunk.anchor_source_s - chunk.baseline_source_s), 3),
            "source_window": [round(window_start, 3), round(window_end, 3)],
            "planned_source_window": [round(window_start, 3), round(window_end, 3)],
            "selected_shot_ids": [],
            "source_jump_s": None,
            "replaced": False,
        }
        if anchor is None:
            diagnostic["skip_reason"] = "anchor shot missing"
            diagnostics.append(diagnostic)
            continue
        min_anchor_score = FLEXIBLE_MIN_ANCHOR_SCORE if flexible_mode else MIN_ANCHOR_SCORE
        if anchor_score + 1e-9 < min_anchor_score:
            diagnostic["skip_reason"] = "anchor score below threshold"
            diagnostics.append(diagnostic)
            continue
        if not flexible_mode and abs(chunk.anchor_source_s - chunk.baseline_source_s) + 1e-9 < MIN_BASELINE_SHIFT_S:
            diagnostic["skip_reason"] = "anchor too close to linear baseline"
            diagnostics.append(diagnostic)
            continue

        local_candidates = [
            shot
            for shot in shots
            if shot.is_story
            and (shot.is_usable or (allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)))
            and shot.tc_start < window_end
            and window_start < shot.tc_end
        ]
        capacity = sum(
            max(0.0, min(shot.tc_end, window_end) - max(shot.tc_start, window_start))
            for shot in local_candidates
        )
        diagnostic["candidate_capacity_s"] = round(capacity, 3)
        if capacity + 1e-6 < replacement_duration:
            diagnostic["skip_reason"] = "local source window lacks footage"
            diagnostics.append(diagnostic)
            continue

        local_placements = fill_local_window(
            beat_id=beat.beat_id,
            tl_start=chunk.tl_start,
            tl_end=replacement_tl_end,
            window_start=window_start,
            window_end=window_end,
            shots=local_candidates,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
        )
        filled_duration = sum(placement.tl_end - placement.tl_start for placement in local_placements)
        if abs(filled_duration - replacement_duration) > 0.02:
            diagnostic["skip_reason"] = "local fill did not cover chunk"
            diagnostics.append(diagnostic)
            continue
        baseline_chunk_drift_s = max_source_drift_for_range(
            beat=beat,
            timing=timing,
            placements=baseline_placements,
            tl_start=chunk.tl_start,
            tl_end=replacement_tl_end,
            source_intervals=source_intervals if flexible_mode else None,
            source_interval_weights=source_interval_weights if flexible_mode else None,
        )
        replacement_chunk_drift_s = max_source_drift_for_range(
            beat=beat,
            timing=timing,
            placements=local_placements,
            tl_start=chunk.tl_start,
            tl_end=replacement_tl_end,
            source_intervals=source_intervals if flexible_mode else None,
            source_interval_weights=source_interval_weights if flexible_mode else None,
        )
        diagnostic["baseline_chunk_drift_s"] = round(baseline_chunk_drift_s, 3)
        diagnostic["replacement_chunk_drift_s"] = round(replacement_chunk_drift_s, 3)
        if flexible_mode and replacement_chunk_drift_s + 1e-6 >= baseline_chunk_drift_s - FLEXIBLE_MIN_DRIFT_IMPROVEMENT_S:
            diagnostic["skip_reason"] = (
                f"replacement drift {replacement_chunk_drift_s:.3f}s did not improve "
                f"local baseline {baseline_chunk_drift_s:.3f}s by at least {FLEXIBLE_MIN_DRIFT_IMPROVEMENT_S:.3f}s"
            )
            diagnostics.append(diagnostic)
            continue
        diagnostic["selected_shot_ids"] = [placement.shot_index for placement in local_placements]
        diagnostic["source_window"] = [
            round(min(placement.src_in for placement in local_placements), 3),
            round(max(placement.src_out for placement in local_placements), 3),
        ]
        source_start = float(diagnostic["source_window"][0])
        source_end = float(diagnostic["source_window"][1])
        source_midpoint = source_start + (source_end - source_start) / 2
        source_jump_s = 0.0 if previous_selected_source_midpoint is None else abs(source_midpoint - previous_selected_source_midpoint)
        expected_source_jump_s = (
            0.0
            if previous_selected_expected_source is None
            else abs(chunk.baseline_source_s - previous_selected_expected_source)
        )
        unexpected_source_jump_s = max(0.0, source_jump_s - expected_source_jump_s)
        diagnostic["source_jump_s"] = round(source_jump_s, 3)
        diagnostic["expected_source_jump_s"] = round(expected_source_jump_s, 3)
        diagnostic["unexpected_source_jump_s"] = round(unexpected_source_jump_s, 3)
        if flexible_mode and unexpected_source_jump_s > FLEXIBLE_MAX_SOURCE_JUMP_S + 1e-6:
            diagnostic["skip_reason"] = f"unexpected source jump exceeds hard cap {FLEXIBLE_MAX_SOURCE_JUMP_S:.3f}s"
            diagnostics.append(diagnostic)
            continue
        previous_selected_source_midpoint = source_midpoint
        previous_selected_expected_source = chunk.baseline_source_s
        diagnostic["replaced"] = True
        diagnostics.append(diagnostic)
        replacements.extend(local_placements)
        replaced_ranges.append((chunk.tl_start, replacement_tl_end))

    baseline_max_drift_s = baseline_max_source_drift(
        beat,
        timing,
        baseline_placements,
        source_intervals=source_intervals if flexible_mode else None,
        source_interval_weights=source_interval_weights if flexible_mode else None,
    )
    baseline_warning_count = alignment_quality_warning_count(
        beat=beat,
        timing=timing,
        placements=baseline_placements,
        max_source_drift_s=max_source_drift_s,
        min_visual_clip=min_visual_clip,
        source_intervals=source_intervals if flexible_mode else None,
        source_interval_weights=source_interval_weights if flexible_mode else None,
    )
    max_source_jump_s = max(
        (
            float(diagnostic.get("source_jump_s", 0.0) or 0.0)
            for diagnostic in diagnostics
            if diagnostic.get("source_jump_s") is not None
        ),
        default=0.0,
    )
    fatal_reason = next(
        (
            str(diagnostic.get("skip_reason"))
            for diagnostic in diagnostics
            if "source jump exceeds hard cap" in str(diagnostic.get("skip_reason", ""))
        ),
        None,
    )
    if flexible_mode and fatal_reason:
        rejected_reason = f"weak chunk: {fatal_reason}"
        mark_guarded_rejection(diagnostics, rejected_reason)
        return IntraBeatAlignmentResult(
            baseline_placements,
            diagnostics,
            [],
            warnings,
            accepted=False,
            rejected_reason=rejected_reason,
            baseline_max_drift_s=baseline_max_drift_s,
            refined_max_drift_s=None,
            baseline_warning_count=baseline_warning_count,
            refined_warning_count=None,
            max_source_jump_s=max_source_jump_s,
        )
    if not replacements:
        weak_reason = next((str(diagnostic.get("skip_reason")) for diagnostic in diagnostics if diagnostic.get("skip_reason")), None)
        rejected_reason = f"weak chunk: {weak_reason}" if flexible_mode and weak_reason else ("no replacement chunks selected" if flexible_mode else None)
        return IntraBeatAlignmentResult(
            baseline_placements,
            diagnostics,
            [],
            warnings,
            accepted=False,
            rejected_reason=rejected_reason,
            baseline_max_drift_s=baseline_max_drift_s,
            refined_max_drift_s=None,
            baseline_warning_count=baseline_warning_count,
            refined_warning_count=None,
            max_source_jump_s=max_source_jump_s,
        )
    try:
        spliced = splice_placements(
            baseline_placements=baseline_placements,
            replacements=replacements,
            replaced_ranges=replaced_ranges,
            min_visual_clip=min_visual_clip,
        )
    except ValueError as exc:
        if not flexible_mode:
            warnings.append(f"beat {beat.beat_id} {mode} intra-beat alignment skipped: {exc}")
            return IntraBeatAlignmentResult(baseline_placements, diagnostics, [], warnings)
        rejected_reason = f"splice failure: {exc}"
        mark_guarded_rejection(diagnostics, rejected_reason)
        return IntraBeatAlignmentResult(
            baseline_placements,
            diagnostics,
            [],
            warnings,
            accepted=False,
            rejected_reason=rejected_reason,
            baseline_max_drift_s=baseline_max_drift_s,
            refined_max_drift_s=None,
            baseline_warning_count=baseline_warning_count,
            refined_warning_count=None,
            max_source_jump_s=max_source_jump_s,
        )
    refined_max_drift_s = baseline_max_source_drift(
        beat,
        timing,
        spliced,
        source_intervals=source_intervals if flexible_mode else None,
        source_interval_weights=source_interval_weights if flexible_mode else None,
    )
    refined_warning_count = alignment_quality_warning_count(
        beat=beat,
        timing=timing,
        placements=spliced,
        max_source_drift_s=max_source_drift_s,
        min_visual_clip=min_visual_clip,
        source_intervals=source_intervals if flexible_mode else None,
        source_interval_weights=source_interval_weights if flexible_mode else None,
    )
    if flexible_mode:
        accepted, rejected_reason = should_accept_guarded_refinement(
            baseline_max_drift_s=baseline_max_drift_s,
            refined_max_drift_s=refined_max_drift_s,
            baseline_warning_count=baseline_warning_count,
            refined_warning_count=refined_warning_count,
        )
        if not accepted:
            mark_guarded_rejection(diagnostics, rejected_reason)
            return IntraBeatAlignmentResult(
                baseline_placements,
                diagnostics,
                [],
                warnings,
                accepted=False,
                rejected_reason=rejected_reason,
                baseline_max_drift_s=baseline_max_drift_s,
                refined_max_drift_s=refined_max_drift_s,
                baseline_warning_count=baseline_warning_count,
                refined_warning_count=refined_warning_count,
                max_source_jump_s=max_source_jump_s,
            )
        return IntraBeatAlignmentResult(
            spliced,
            diagnostics,
            merge_ranges(replaced_ranges),
            warnings,
            accepted=True,
            rejected_reason=None,
            baseline_max_drift_s=baseline_max_drift_s,
            refined_max_drift_s=refined_max_drift_s,
            baseline_warning_count=baseline_warning_count,
            refined_warning_count=refined_warning_count,
            max_source_jump_s=max_source_jump_s,
        )
    if mode == "long_beat":
        rejected_reason = None
        if placements_have_source_order_mismatch(spliced):
            rejected_reason = "refined source order mismatch"
        else:
            accepted, rejected_reason = should_accept_guarded_refinement(
                baseline_max_drift_s=baseline_max_drift_s,
                refined_max_drift_s=refined_max_drift_s,
                baseline_warning_count=baseline_warning_count,
                refined_warning_count=refined_warning_count,
            )
            if accepted:
                rejected_reason = None
        if rejected_reason:
            mark_guarded_rejection(diagnostics, rejected_reason)
            return IntraBeatAlignmentResult(
                baseline_placements,
                diagnostics,
                [],
                warnings,
                accepted=False,
                rejected_reason=rejected_reason,
                baseline_max_drift_s=baseline_max_drift_s,
                refined_max_drift_s=refined_max_drift_s,
                baseline_warning_count=baseline_warning_count,
                refined_warning_count=refined_warning_count,
                max_source_jump_s=max_source_jump_s,
            )
    warnings.append(
        f"beat {beat.beat_id} {mode}_intra_beat_align replaced "
        + ", ".join(f"{start:.3f}-{end:.3f}s" for start, end in merge_ranges(replaced_ranges))
    )
    return IntraBeatAlignmentResult(
        spliced,
        diagnostics,
        merge_ranges(replaced_ranges),
        warnings,
        accepted=True,
        rejected_reason=None,
        baseline_max_drift_s=baseline_max_drift_s,
        refined_max_drift_s=refined_max_drift_s,
        baseline_warning_count=baseline_warning_count,
        refined_warning_count=refined_warning_count,
        max_source_jump_s=max_source_jump_s,
    )


def plan_long_beat_source_windows(
    beat: ReviewBeat,
    chunks: list[AlignmentChunk],
    shots_by_index: dict[int, Shot],
) -> dict[int, tuple[float, float]]:
    windows: dict[int, tuple[float, float]] = {}
    next_boundary = beat.src_tc_end
    for index in range(len(chunks) - 1, -1, -1):
        chunk = chunks[index]
        anchor = shots_by_index.get(chunk.anchor_shot_index)
        if anchor is None:
            continue
        window_end = next_boundary
        window_start = max(
            beat.src_tc_start,
            min(anchor.tc_start, window_end - chunk.duration),
        )
        if window_end - window_start + 1e-6 < chunk.duration:
            window_start = max(beat.src_tc_start, window_end - chunk.duration)
        windows[index] = (window_start, window_end)
        next_boundary = window_start
    return windows

def plan_flexible_source_window(
    *,
    beat: ReviewBeat,
    anchor: Shot,
    replacement_duration: float,
    shots: list[Shot],
    max_clip: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
) -> tuple[float, float]:
    window_start = max(beat.src_tc_start, anchor.tc_start)
    window_end = min(
        beat.src_tc_end,
        max(anchor.tc_end, window_start + max(max_clip, replacement_duration)),
    )

    def eligible(shot: Shot) -> bool:
        return shot.is_story and (
            shot.is_usable or (allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip))
        )

    def capacity(start: float, end: float) -> float:
        return sum(
            max(0.0, min(shot.tc_end, end) - max(shot.tc_start, start))
            for shot in shots
            if eligible(shot) and shot.tc_start < end and start < shot.tc_end
        )

    step = max(max_clip, min_visual_clip, 1.0)
    while capacity(window_start, window_end) + 1e-6 < replacement_duration and window_end < beat.src_tc_end - 1e-6:
        window_end = min(beat.src_tc_end, window_end + step)
    while capacity(window_start, window_end) + 1e-6 < replacement_duration and window_start > beat.src_tc_start + 1e-6:
        window_start = max(beat.src_tc_start, window_start - step)
    return window_start, window_end


def merge_low_confidence_transitions(
    beat: ReviewBeat,
    chunks: list[AlignmentChunk],
) -> list[AlignmentChunk]:
    output: list[AlignmentChunk] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        if index + 1 < len(chunks):
            next_chunk = chunks[index + 1]
            chunk_score = alignment_anchor_score(beat, chunk)
            next_score = alignment_anchor_score(beat, next_chunk)
            if (
                chunk_score + 1e-9 < MIN_ANCHOR_SCORE
                and next_score + 1e-9 >= MIN_ANCHOR_SCORE
                and next_chunk.tl_end - chunk.tl_start <= MAX_CHUNK_DURATION_S + 1e-6
            ):
                output.append(merge_chunks(chunk, next_chunk, anchor_from_right=True))
                index += 2
                continue
        output.append(chunk)
        index += 1
    return output


def alignment_anchor_score(beat: ReviewBeat, chunk: AlignmentChunk) -> float:
    chronology_prior = max(
        0.0,
        1.0 - abs(chunk.anchor_source_s - chunk.baseline_source_s) / max(beat.src_tc_end - beat.src_tc_start, 1e-6),
    )
    return chunk.semantic_score + CHRONOLOGY_PRIOR_WEIGHT * chronology_prior


def alignment_quality_warning_count(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    placements: list[EdlPlacement],
    max_source_drift_s: float,
    min_visual_clip: float,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> int:
    warnings = 0
    if (
        baseline_max_source_drift(
            beat,
            timing,
            placements,
            source_intervals=source_intervals,
            source_interval_weights=source_interval_weights,
        )
        > max_source_drift_s + 1e-6
    ):
        warnings += 1
    warnings += sum(
        1
        for placement in placements
        if placement.tl_end - placement.tl_start + 1e-6 < min_visual_clip
    )
    return warnings

def placements_have_source_order_mismatch(placements: list[EdlPlacement], *, tolerance_s: float = 0.08) -> bool:
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.src_in))
    return any(ordered[index].src_in > ordered[index + 1].src_in + tolerance_s for index in range(len(ordered) - 1))


def should_accept_guarded_refinement(
    *,
    baseline_max_drift_s: float,
    refined_max_drift_s: float,
    baseline_warning_count: int,
    refined_warning_count: int,
) -> tuple[bool, str | None]:
    if refined_max_drift_s + 1e-6 >= baseline_max_drift_s - FLEXIBLE_MIN_DRIFT_IMPROVEMENT_S:
        return False, (
            f"refined drift {refined_max_drift_s:.3f}s did not improve baseline "
            f"{baseline_max_drift_s:.3f}s by at least {FLEXIBLE_MIN_DRIFT_IMPROVEMENT_S:.3f}s"
        )
    if refined_warning_count > baseline_warning_count:
        return False, f"refined warning count {refined_warning_count} > baseline {baseline_warning_count}"
    return True, None


def mark_guarded_rejection(diagnostics: list[dict[str, object]], reason: str | None) -> None:
    for diagnostic in diagnostics:
        diagnostic["accepted"] = False
        diagnostic["rejection_reason"] = reason
        if diagnostic.get("replaced"):
            diagnostic["replaced"] = False


def apply_opening_intra_beat_alignment(**kwargs) -> IntraBeatAlignmentResult:  # type: ignore[no-untyped-def]
    return apply_intra_beat_alignment(**kwargs, mode="opening")


def baseline_max_source_drift(
    beat: ReviewBeat,
    timing: BeatTiming,
    placements: list[EdlPlacement],
    *,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> float:
    max_drift = 0.0
    for placement in placements:
        max_drift = max(
            max_drift,
            placement_max_sampled_drift(
                beat,
                timing,
                placement,
                source_intervals=source_intervals,
                source_interval_weights=source_interval_weights,
            ),
        )
    return max_drift


def max_source_drift_for_range(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    placements: list[EdlPlacement],
    tl_start: float,
    tl_end: float,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> float:
    source_span = beat.src_tc_end - beat.src_tc_start
    if tl_end <= tl_start or source_span <= 0:
        return 0.0
    sample_count = max(3, int(ceil((tl_end - tl_start) / 8.0)))
    max_drift = 0.0
    for index in range(sample_count + 1):
        sample_tl = tl_start + (tl_end - tl_start) * index / max(sample_count, 1)
        expected = expected_source_at_tl(
            beat=beat,
            timing=timing,
            sample_tl=sample_tl,
            source_intervals=source_intervals,
            source_interval_weights=source_interval_weights,
        )
        best_drift = None
        for placement in placements:
            if placement.tl_start - 1e-6 <= sample_tl <= placement.tl_end + 1e-6:
                drift = source_drift_at_tl(placement, sample_tl, expected)
                best_drift = drift if best_drift is None else min(best_drift, drift)
        if best_drift is None:
            continue
        max_drift = max(max_drift, best_drift)
    return max_drift


def placement_max_sampled_drift(
    beat: ReviewBeat,
    timing: BeatTiming,
    placement: EdlPlacement,
    *,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> float:
    sample_count = max(3, int(ceil((placement.tl_end - placement.tl_start) / 8.0)))
    max_drift = 0.0
    for index in range(sample_count + 1):
        sample_tl = placement.tl_start + (placement.tl_end - placement.tl_start) * index / max(sample_count, 1)
        expected = expected_source_at_tl(
            beat=beat,
            timing=timing,
            sample_tl=sample_tl,
            source_intervals=source_intervals,
            source_interval_weights=source_interval_weights,
        )
        max_drift = max(max_drift, source_drift_at_tl(placement, sample_tl, expected))
    return max_drift


def expected_source_at_tl(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    sample_tl: float,
    source_intervals: list[tuple[float, float]] | None,
    source_interval_weights: list[float] | None,
) -> float:
    progress = min(1.0, max(0.0, (sample_tl - timing.tl_start) / max(timing.duration, 1e-6)))
    return flexible_expected_source_position(
        beat=beat,
        progress=progress,
        source_intervals=source_intervals,
        source_interval_weights=source_interval_weights,
    )


def source_drift_at_tl(placement: EdlPlacement, sample_tl: float, expected_src_position: float) -> float:
    if placement.tl_end <= placement.tl_start + 1e-6:
        source_position = placement.src_in
    else:
        progress = min(1.0, max(0.0, (sample_tl - placement.tl_start) / (placement.tl_end - placement.tl_start)))
        source_position = placement.src_in + (placement.src_out - placement.src_in) * progress
    return abs(source_position - expected_src_position)


def long_beat_alignment_required(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    placements: list[EdlPlacement],
    max_source_drift_s: float,
    source_intervals: list[tuple[float, float]] | None = None,
    source_interval_weights: list[float] | None = None,
) -> tuple[bool, float]:
    source_audio_ratio = (beat.src_tc_end - beat.src_tc_start) / max(timing.duration, 1e-6)
    drift = baseline_max_source_drift(
        beat,
        timing,
        placements,
        source_intervals=source_intervals,
        source_interval_weights=source_interval_weights,
    )
    threshold = max(LONG_BEAT_MIN_DRIFT_S, max_source_drift_s * LONG_BEAT_DRIFT_MULTIPLIER)
    return source_audio_ratio >= LONG_BEAT_MIN_SOURCE_AUDIO_RATIO and drift > threshold + 1e-6, drift


def apply_hook_leading_brightness_guard(
    *,
    beat: ReviewBeat,
    baseline_placements: list[EdlPlacement],
    shots: list[Shot],
    min_brightness: float,
    max_clip: float,
    min_visual_clip: float,
) -> HookLeadingGuardResult:
    ordered = sorted(baseline_placements, key=lambda item: (item.tl_start, item.tl_end))
    if not beat.is_hook or min_brightness <= 0 or not ordered:
        return HookLeadingGuardResult(ordered, False, None, [], [])
    shots_by_index = {shot.index: shot for shot in shots}
    first = ordered[0]
    first_shot = shots_by_index.get(first.shot_index)
    if first_shot is None or first_shot.brightness + 1e-9 >= min_brightness:
        return HookLeadingGuardResult(ordered, False, first.shot_index, [], [])
    window_end = ordered[1].src_in if len(ordered) > 1 and ordered[1].src_in > first.src_in else beat.src_tc_end
    candidates = [
        shot
        for shot in shots
        if shot.is_story
        and shot.is_usable
        and shot.brightness + 1e-9 >= min_brightness
        and shot.tc_start < window_end
        and first.src_in < shot.tc_end
    ]
    replacements = fill_local_window(
        beat_id=beat.beat_id,
        tl_start=first.tl_start,
        tl_end=first.tl_end,
        window_start=first.src_in,
        window_end=window_end,
        shots=candidates,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    duration = sum(item.tl_end - item.tl_start for item in replacements)
    if abs(duration - (first.tl_end - first.tl_start)) > 0.02:
        warning = f"beat {beat.beat_id} hook leading brightness guard could not replace shot {first.shot_index}"
        return HookLeadingGuardResult(ordered, False, first.shot_index, [], [warning])
    try:
        guarded = splice_placements(
            baseline_placements=ordered,
            replacements=replacements,
            replaced_ranges=[(first.tl_start, first.tl_end)],
            min_visual_clip=min_visual_clip,
        )
    except ValueError as exc:
        warning = f"beat {beat.beat_id} hook leading brightness guard skipped: {exc}"
        return HookLeadingGuardResult(ordered, False, first.shot_index, [], [warning])
    replacement_ids = [item.shot_index for item in replacements]
    warning = (
        f"beat {beat.beat_id} hook_leading_brightness_guard replaced shot {first.shot_index} "
        f"with {replacement_ids} at threshold {min_brightness:.3f}"
    )
    return HookLeadingGuardResult(guarded, True, first.shot_index, replacement_ids, [warning])


def fill_local_window(
    *,
    beat_id: int,
    tl_start: float,
    tl_end: float,
    window_start: float,
    window_end: float,
    shots: list[Shot],
    max_clip: float,
    min_visual_clip: float,
) -> list[EdlPlacement]:
    remaining = tl_end - tl_start
    timeline_cursor = tl_start
    output: list[EdlPlacement] = []
    ordered_shots = sorted(shots, key=lambda item: (item.tc_start, item.index))
    for shot_position, shot in enumerate(ordered_shots):
        source_start = max(shot.tc_start, window_start)
        source_end = min(shot.tc_end, window_end)
        available = source_end - source_start
        if available + 1e-6 < min_visual_clip:
            continue
        take = min(available, remaining)
        remainder = remaining - take
        reserved_tail = min_visual_clip + 0.001
        if 1e-6 < remainder < reserved_tail - 1e-6:
            future_capacity = sum(
                max(0.0, min(candidate.tc_end, window_end) - max(candidate.tc_start, window_start))
                for candidate in ordered_shots[shot_position + 1 :]
            )
            adjustment = reserved_tail - remainder
            if future_capacity + 1e-6 >= reserved_tail and take - adjustment + 1e-6 >= min_visual_clip:
                take -= adjustment
        if take <= 1e-6:
            continue
        finishes_timeline = take >= remaining - 1e-6
        chunk_count = max(1, ceil(take / max_clip - 1e-9))
        chunk_duration = take / chunk_count
        source_cursor = source_start
        for chunk_index in range(chunk_count):
            duration = remaining if chunk_index == chunk_count - 1 and remaining < chunk_duration + 1e-6 else chunk_duration
            duration = min(duration, source_end - source_cursor, remaining)
            if duration <= 1e-6:
                continue
            placement_tl_end = tl_end if finishes_timeline and chunk_index == chunk_count - 1 else round(timeline_cursor + duration, 3)
            actual_duration = placement_tl_end - timeline_cursor
            output.append(
                EdlPlacement(
                    tl_start=round(timeline_cursor, 3),
                    tl_end=round(placement_tl_end, 3),
                    src=shot.src,
                    src_in=round(source_cursor, 3),
                    src_out=round(source_cursor + actual_duration, 3),
                    beat_id=beat_id,
                    shot_index=shot.index,
                    reused=False,
                    speed=1.0,
                )
            )
            timeline_cursor = placement_tl_end
            source_cursor += actual_duration
            remaining = round(remaining - actual_duration, 6)
        if remaining <= 1e-6:
            break
    return output


def splice_placements(
    *,
    baseline_placements: list[EdlPlacement],
    replacements: list[EdlPlacement],
    replaced_ranges: list[tuple[float, float]],
    min_visual_clip: float,
) -> list[EdlPlacement]:
    ranges = adjust_splice_ranges(
        baseline_placements=baseline_placements,
        replaced_ranges=replaced_ranges,
        min_visual_clip=min_visual_clip,
    )
    replacements = trim_replacements_to_ranges(
        replacements=replacements,
        ranges=ranges,
        min_visual_clip=min_visual_clip,
    )
    output: list[EdlPlacement] = []
    for placement in baseline_placements:
        pieces = [(placement.tl_start, placement.tl_end)]
        for range_start, range_end in ranges:
            next_pieces: list[tuple[float, float]] = []
            for start, end in pieces:
                if end <= range_start + 1e-6 or start >= range_end - 1e-6:
                    next_pieces.append((start, end))
                    continue
                if start < range_start - 1e-6:
                    next_pieces.append((start, range_start))
                if end > range_end + 1e-6:
                    next_pieces.append((range_end, end))
            pieces = next_pieces
        for start, end in pieces:
            if end - start + 1e-6 < min_visual_clip:
                raise ValueError(f"splice would create a {end - start:.3f}s baseline remainder")
            output.append(slice_placement(placement, start, end))
    output.extend(replacements)
    ordered = sorted(output, key=lambda item: (item.tl_start, item.tl_end, item.shot_index))
    for previous, current in zip(ordered, ordered[1:]):
        if abs(previous.tl_end - current.tl_start) > 1e-3:
            raise ValueError("splice created a timeline gap or overlap")
    return ordered


def adjust_splice_ranges(
    *,
    baseline_placements: list[EdlPlacement],
    replaced_ranges: list[tuple[float, float]],
    min_visual_clip: float,
) -> list[tuple[float, float]]:
    adjusted: list[tuple[float, float]] = []
    for original_start, original_end in merge_ranges(replaced_ranges):
        range_start = original_start
        range_end = original_end
        for placement in baseline_placements:
            if placement.tl_start + 1e-6 < range_start < placement.tl_end - 1e-6:
                left_remainder = range_start - placement.tl_start
                if left_remainder + 1e-6 < min_visual_clip:
                    range_start = placement.tl_start + min_visual_clip
            if placement.tl_start + 1e-6 < range_end < placement.tl_end - 1e-6:
                right_remainder = placement.tl_end - range_end
                if right_remainder + 1e-6 < min_visual_clip:
                    range_end = placement.tl_end - min_visual_clip
        if range_end - range_start + 1e-6 < min_visual_clip:
            raise ValueError("adjusted splice range is shorter than min_visual_clip")
        adjusted.append((round(range_start, 3), round(range_end, 3)))
    return merge_ranges(adjusted)


def trim_replacements_to_ranges(
    *,
    replacements: list[EdlPlacement],
    ranges: list[tuple[float, float]],
    min_visual_clip: float,
) -> list[EdlPlacement]:
    trimmed: list[EdlPlacement] = []
    for replacement in replacements:
        for range_start, range_end in ranges:
            start = max(replacement.tl_start, range_start)
            end = min(replacement.tl_end, range_end)
            if end <= start + 1e-6:
                continue
            if end - start + 1e-6 < min_visual_clip:
                raise ValueError(f"range adjustment would create a {end - start:.3f}s replacement")
            trimmed.append(slice_placement(replacement, start, end))
    return trimmed


def slice_placement(placement: EdlPlacement, tl_start: float, tl_end: float) -> EdlPlacement:
    if abs(tl_start - placement.tl_start) <= 1e-6 and abs(tl_end - placement.tl_end) <= 1e-6:
        return placement
    source_start = placement.src_in + (tl_start - placement.tl_start) * placement.speed
    source_end = placement.src_in + (tl_end - placement.tl_start) * placement.speed
    return placement.model_copy(
        update={
            "tl_start": round(tl_start, 3),
            "tl_end": round(tl_end, 3),
            "src_in": round(source_start, 3),
            "src_out": round(source_end, 3),
        }
    )


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if output and start <= output[-1][1] + 1e-3:
            output[-1] = (output[-1][0], max(output[-1][1], end))
        else:
            output.append((start, end))
    return output


def recompute_reuse_flags(
    placements: list[EdlPlacement],
    reuse_counts_before: dict[int, int],
) -> list[EdlPlacement]:
    counts = dict(reuse_counts_before)
    output: list[EdlPlacement] = []
    previous: EdlPlacement | None = None
    for placement in sorted(placements, key=lambda item: (item.tl_start, item.tl_end)):
        continuation = (
            previous is not None
            and previous.shot_index == placement.shot_index
            and abs(previous.tl_end - placement.tl_start) <= 1e-3
            and abs(previous.src_out - placement.src_in) <= 1e-3
        )
        reused = previous.reused if continuation and previous is not None else counts.get(placement.shot_index, 0) > 0
        output.append(placement.model_copy(update={"reused": reused}))
        if not continuation:
            counts[placement.shot_index] = counts.get(placement.shot_index, 0) + 1
        previous = placement
    return output


def update_reuse_counts(
    reuse_counts: dict[int, int],
    reuse_counts_before: dict[int, int],
    placements: list[EdlPlacement],
) -> None:
    reuse_counts.clear()
    reuse_counts.update(reuse_counts_before)
    previous: EdlPlacement | None = None
    for placement in placements:
        continuation = (
            previous is not None
            and previous.shot_index == placement.shot_index
            and abs(previous.tl_end - placement.tl_start) <= 1e-3
            and abs(previous.src_out - placement.src_in) <= 1e-3
        )
        if not continuation:
            reuse_counts[placement.shot_index] = reuse_counts.get(placement.shot_index, 0) + 1
        previous = placement
