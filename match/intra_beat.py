from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.candidates import is_dark_fallback_candidate


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u2026])\s+")
ENTITY_TOKEN_RE = re.compile(r"[\w\u00c0-\u1ef9]+(?:-[\w\u00c0-\u1ef9]+)*", re.UNICODE)
ENTITY_STOPWORDS = {"anh", "cô", "gã", "người", "trước", "trên", "đúng", "ngay", "sau"}
MIN_AUDIO_DURATION_S = 45.0
MIN_SOURCE_AUDIO_RATIO = 3.0
ANALYSIS_DURATION_S = 30.0
MIN_SENTENCE_COUNT = 4
MIN_ANCHOR_SCORE = 0.50
MIN_BASELINE_SHIFT_S = 6.0
CHRONOLOGY_PRIOR_WEIGHT = 0.10
SHORT_SENTENCE_S = 3.0
MAX_CHUNK_DURATION_S = 10.0
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

    @property
    def used(self) -> bool:
        return bool(self.replaced_ranges)


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
) -> list[AlignmentChunk]:
    candidates = sorted(
        [
            shot
            for shot in shots
            if shot.is_story
            and shot.is_usable
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
            semantic_row.append(semantic_score)
            score_row.append(semantic_score + chronology_prior_weight * chronology_prior)
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


def apply_opening_intra_beat_alignment(
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
) -> IntraBeatAlignmentResult:
    chunks = select_monotonic_anchors(
        beat=beat,
        timing=timing,
        sentences=sentences,
        shots=shots,
        query_shot_scores=query_shot_scores,
    )
    shots_by_index = {shot.index: shot for shot in shots}
    diagnostics: list[dict[str, object]] = []
    replacements: list[EdlPlacement] = []
    replaced_ranges: list[tuple[float, float]] = []
    warnings: list[str] = []
    source_audio_ratio = (beat.src_tc_end - beat.src_tc_start) / timing.duration

    for index, chunk in enumerate(chunks):
        anchor = shots_by_index.get(chunk.anchor_shot_index)
        next_anchor = shots_by_index.get(chunks[index + 1].anchor_shot_index) if index + 1 < len(chunks) else None
        replacement_tl_end = chunk.tl_end
        matching_tail = next(
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
        if matching_tail is not None:
            window_end = matching_tail.src_in
            window_start = max(beat.src_tc_start, window_end - replacement_duration)
        else:
            window_start = anchor.tc_start if anchor is not None else beat.src_tc_start
            if next_anchor is not None and next_anchor.tc_start > window_start:
                window_end = next_anchor.tc_start
            else:
                window_end = min(beat.src_tc_end, window_start + max(max_clip, replacement_duration * source_audio_ratio))
        chronology_prior = max(
            0.0,
            1.0 - abs(chunk.anchor_source_s - chunk.baseline_source_s) / max(beat.src_tc_end - beat.src_tc_start, 1e-6),
        )
        anchor_score = chunk.semantic_score + CHRONOLOGY_PRIOR_WEIGHT * chronology_prior
        diagnostic: dict[str, object] = {
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
            "selected_shot_ids": [],
            "replaced": False,
        }
        if anchor is None:
            diagnostic["skip_reason"] = "anchor shot missing"
            diagnostics.append(diagnostic)
            continue
        if anchor_score + 1e-9 < MIN_ANCHOR_SCORE:
            diagnostic["skip_reason"] = "anchor score below threshold"
            diagnostics.append(diagnostic)
            continue
        if abs(chunk.anchor_source_s - chunk.baseline_source_s) + 1e-9 < MIN_BASELINE_SHIFT_S:
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
        diagnostic["selected_shot_ids"] = [placement.shot_index for placement in local_placements]
        diagnostic["replaced"] = True
        diagnostics.append(diagnostic)
        replacements.extend(local_placements)
        replaced_ranges.append((chunk.tl_start, replacement_tl_end))

    if not replacements:
        return IntraBeatAlignmentResult(baseline_placements, diagnostics, [], warnings)
    try:
        spliced = splice_placements(
            baseline_placements=baseline_placements,
            replacements=replacements,
            replaced_ranges=replaced_ranges,
            min_visual_clip=min_visual_clip,
        )
    except ValueError as exc:
        warnings.append(f"beat {beat.beat_id} opening intra-beat alignment skipped: {exc}")
        return IntraBeatAlignmentResult(baseline_placements, diagnostics, [], warnings)
    warnings.append(
        f"beat {beat.beat_id} opening_intra_beat_align replaced "
        + ", ".join(f"{start:.3f}-{end:.3f}s" for start, end in merge_ranges(replaced_ranges))
    )
    return IntraBeatAlignmentResult(spliced, diagnostics, merge_ranges(replaced_ranges), warnings)


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
    for shot in sorted(shots, key=lambda item: (item.tc_start, item.index)):
        source_start = max(shot.tc_start, window_start)
        source_end = min(shot.tc_end, window_end)
        available = source_end - source_start
        if available + 1e-6 < min_visual_clip:
            continue
        take = min(available, remaining)
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
    ranges = merge_ranges(replaced_ranges)
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
