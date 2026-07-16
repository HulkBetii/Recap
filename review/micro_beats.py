from __future__ import annotations

import re
from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat

DEFAULT_HARD_MAX_AUDIO_S = 25.0
DEFAULT_HOOK_WARN_AUDIO_S = 30.0
DEFAULT_SOURCE_DENSE_RATIO = 2.3
DEFAULT_TARGET_SOURCE_SPAN_S = 35.0

_SENTENCE_RE = re.compile(r"(?<=[.!?\u2026\u3002\uff01\uff1f])\s+")
_EPSILON = 1e-6


@dataclass(frozen=True)
class BeatAudioStats:
    max_est_beat_audio_s: float
    avg_est_beat_audio_s: float
    n_beats_over_max_audio: int


@dataclass(frozen=True)
class MicroBeatReport:
    n_split_beats: int
    split_beat_ids: list[int]
    warnings: list[str]
    max_est_beat_audio_s: float
    avg_est_beat_audio_s: float
    n_beats_over_max_audio: int


def split_long_beats(
    beats: list[ReviewBeat],
    film_map: list[FilmMapSegment],
    *,
    max_audio_s: float,
    target_audio_s: float,
    tts_cps: float,
    enabled: bool,
    hard_max_audio_s: float = DEFAULT_HARD_MAX_AUDIO_S,
    hook_warn_audio_s: float = DEFAULT_HOOK_WARN_AUDIO_S,
    _refinement_passes: int = 1,
) -> tuple[list[ReviewBeat], MicroBeatReport]:
    if max_audio_s <= 0 or target_audio_s <= 0 or tts_cps <= 0 or hard_max_audio_s <= 0:
        return beats, MicroBeatReport(0, [], [], 0.0, 0.0, 0)
    if not enabled:
        stats = beat_audio_stats(beats, tts_cps=tts_cps, max_audio_s=max_audio_s)
        return beats, MicroBeatReport(
            0,
            [],
            [],
            stats.max_est_beat_audio_s,
            stats.avg_est_beat_audio_s,
            stats.n_beats_over_max_audio,
        )

    by_id = {segment.id: segment for segment in film_map}
    output: list[ReviewBeat] = []
    split_ids: list[int] = []
    warnings: list[str] = []
    for beat in beats:
        estimated_s = estimate_audio_s(beat.narration, tts_cps)
        source_duration_s = beat.src_tc_end - beat.src_tc_start
        segment_count = beat.to_seg_id - beat.from_seg_id + 1
        sentences = split_sentences(beat.narration)
        source_dense_parts = source_dense_part_count(
            source_duration_s=source_duration_s,
            estimated_audio_s=estimated_s,
            sentence_count=len(sentences),
            segment_count=segment_count,
        )
        needs_audio_split = estimated_s > max_audio_s + _EPSILON
        needs_source_dense_split = source_dense_parts >= 2
        if beat.is_hook:
            if estimated_s > hook_warn_audio_s + _EPSILON:
                warnings.append(
                    f"hook beat {beat.beat_id} estimated {estimated_s:.1f}s exceeds hook warning limit {hook_warn_audio_s:.1f}s; kept unsplit"
                )
            output.append(beat)
            continue
        if not needs_audio_split and not needs_source_dense_split:
            output.append(beat)
            continue
        if segment_count < 2 or len(sentences) < 2:
            if needs_audio_split:
                warnings.append(
                    f"beat {beat.beat_id} estimated {estimated_s:.1f}s exceeds max {max_audio_s:.1f}s but cannot split safely "
                    f"({len(sentences)} sentence(s), {segment_count} source segment(s))"
                )
            elif needs_source_dense_split:
                warnings.append(
                    f"beat {beat.beat_id} source span {source_duration_s:.1f}s is dense for estimated {estimated_s:.1f}s audio "
                    f"but cannot split safely ({len(sentences)} sentence(s), {segment_count} source segment(s))"
                )
            output.append(beat)
            continue

        missing_segments = [segment_id for segment_id in range(beat.from_seg_id, beat.to_seg_id + 1) if segment_id not in by_id]
        if missing_segments:
            warnings.append(f"beat {beat.beat_id} cannot split safely because source segment ids are missing: {missing_segments[:5]}")
            output.append(beat)
            continue

        chunks = chunk_sentences(
            sentences,
            target_audio_s=target_audio_s,
            max_audio_s=max_audio_s,
            hard_max_audio_s=hard_max_audio_s,
            tts_cps=tts_cps,
        )
        if source_dense_parts > len(chunks):
            chunks = chunk_sentences_to_count(sentences, source_dense_parts, tts_cps=tts_cps)
        original_chunk_count = len(chunks)
        if len(chunks) > segment_count:
            chunks = merge_chunks_to_limit(chunks, segment_count, tts_cps=tts_cps)
            warnings.append(
                f"beat {beat.beat_id} merged micro chunks from {original_chunk_count} to {len(chunks)} because only {segment_count} source segment(s) are available"
            )
        if len(chunks) < 2:
            if estimated_s > max_audio_s + _EPSILON:
                warnings.append(f"beat {beat.beat_id} stayed unsplit after guarded chunking; estimated {estimated_s:.1f}s")
            output.append(beat)
            continue

        parts = [" ".join(chunk).strip() for chunk in chunks if chunk]
        weights = [max(estimate_audio_s(text, tts_cps), _EPSILON) for text in parts]
        spans = partition_segments_by_time_weights(beat.from_seg_id, beat.to_seg_id, weights, by_id)
        if len(parts) != len(spans):
            warnings.append(f"beat {beat.beat_id} stayed unsplit because sentence/source partition mismatch")
            output.append(beat)
            continue

        split_ids.append(beat.beat_id)
        reason = "audio"
        if needs_audio_split and needs_source_dense_split:
            reason = "audio+source-dense"
        elif needs_source_dense_split:
            ratio = source_duration_s / max(estimated_s, _EPSILON)
            reason = f"source-dense ratio {ratio:.2f}"
        warnings.append(f"split beat {beat.beat_id} into {len(parts)} micro-beats ({reason})")
        for text, (from_seg_id, to_seg_id) in zip(parts, spans):
            part_estimated_s = estimate_audio_s(text, tts_cps)
            if part_estimated_s > hard_max_audio_s + _EPSILON:
                warnings.append(
                    f"micro-beat from source beat {beat.beat_id} estimated {part_estimated_s:.1f}s exceeds hard max {hard_max_audio_s:.1f}s after split"
                )
            start = by_id[from_seg_id].tc_start
            end = by_id[to_seg_id].tc_end
            output.append(
                ReviewBeat(
                    beat_id=0,
                    narration=text,
                    from_seg_id=from_seg_id,
                    to_seg_id=to_seg_id,
                    src_tc_start=start,
                    src_tc_end=end,
                    is_hook=False,
                )
            )

    normalized = [beat.model_copy(update={"beat_id": index}) for index, beat in enumerate(output)]
    if enabled and _refinement_passes > 0:
        refined, refinement_report = split_long_beats(
            normalized,
            film_map,
            max_audio_s=max_audio_s,
            target_audio_s=target_audio_s,
            tts_cps=tts_cps,
            enabled=True,
            hard_max_audio_s=hard_max_audio_s,
            hook_warn_audio_s=hook_warn_audio_s,
            _refinement_passes=_refinement_passes - 1,
        )
        if len(refined) > len(normalized):
            return refined, MicroBeatReport(
                len(split_ids) + refinement_report.n_split_beats,
                split_ids + refinement_report.split_beat_ids,
                warnings + refinement_report.warnings,
                refinement_report.max_est_beat_audio_s,
                refinement_report.avg_est_beat_audio_s,
                refinement_report.n_beats_over_max_audio,
            )
    stats = beat_audio_stats(normalized, tts_cps=tts_cps, max_audio_s=max_audio_s)
    return normalized, MicroBeatReport(
        len(split_ids),
        split_ids,
        warnings,
        stats.max_est_beat_audio_s,
        stats.avg_est_beat_audio_s,
        stats.n_beats_over_max_audio,
    )


def estimate_audio_s(text: str, tts_cps: float) -> float:
    if tts_cps <= 0:
        return 0.0
    return len(" ".join(text.strip().split())) / tts_cps


def beat_audio_stats(beats: list[ReviewBeat], *, tts_cps: float, max_audio_s: float) -> BeatAudioStats:
    if tts_cps <= 0 or not beats:
        return BeatAudioStats(0.0, 0.0, 0)
    estimates = [estimate_audio_s(beat.narration, tts_cps) for beat in beats]
    return BeatAudioStats(
        max_est_beat_audio_s=round(max(estimates), 3),
        avg_est_beat_audio_s=round(sum(estimates) / len(estimates), 3),
        n_beats_over_max_audio=sum(1 for value in estimates if value > max_audio_s + _EPSILON),
    )


def beat_ids_over_audio_s(beats: list[ReviewBeat], *, tts_cps: float, limit_s: float) -> list[int]:
    if tts_cps <= 0 or limit_s <= 0:
        return []
    return [beat.beat_id for beat in beats if estimate_audio_s(beat.narration, tts_cps) > limit_s + _EPSILON]


def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return []
    parts = [part.strip() for part in _SENTENCE_RE.split(normalized) if part.strip()]
    return parts or [normalized]


def chunk_sentences(
    sentences: list[str],
    *,
    target_audio_s: float,
    max_audio_s: float,
    hard_max_audio_s: float,
    tts_cps: float,
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for sentence in sentences:
        if not current:
            current = [sentence]
            continue
        current_s = estimate_audio_s(" ".join(current), tts_cps)
        candidate = current + [sentence]
        candidate_s = estimate_audio_s(" ".join(candidate), tts_cps)
        if current_s >= target_audio_s and candidate_s > max_audio_s + _EPSILON:
            chunks.append(current)
            current = [sentence]
        elif candidate_s > hard_max_audio_s + _EPSILON:
            chunks.append(current)
            current = [sentence]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def source_dense_part_count(
    *,
    source_duration_s: float,
    estimated_audio_s: float,
    sentence_count: int,
    segment_count: int,
) -> int:
    if (
        source_duration_s <= DEFAULT_TARGET_SOURCE_SPAN_S + _EPSILON
        or estimated_audio_s <= _EPSILON
        or sentence_count < 2
        or segment_count < 2
    ):
        return 0
    source_audio_ratio = source_duration_s / estimated_audio_s
    if source_audio_ratio + _EPSILON < DEFAULT_SOURCE_DENSE_RATIO:
        return 0
    desired = max(2, round(source_duration_s / DEFAULT_TARGET_SOURCE_SPAN_S))
    return max(0, min(sentence_count, segment_count, desired))

def chunk_sentences_to_count(sentences: list[str], count: int, *, tts_cps: float) -> list[list[str]]:
    if not sentences:
        return []
    count = max(1, min(count, len(sentences)))
    if count == 1:
        return [list(sentences)]
    weights = [max(estimate_audio_s(sentence, tts_cps), _EPSILON) for sentence in sentences]
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running)
    total = max(running, _EPSILON)
    chunks: list[list[str]] = []
    cursor = 0
    for part_index in range(count):
        remaining_parts = count - part_index
        if remaining_parts == 1:
            end = len(sentences)
        else:
            target = total * (part_index + 1) / count
            min_end = cursor + 1
            max_end = len(sentences) - (remaining_parts - 1)
            end = min(
                range(min_end, max_end + 1),
                key=lambda candidate: (abs(cumulative[candidate - 1] - target), candidate),
            )
        chunks.append(sentences[cursor:end])
        cursor = end
    return chunks

def merge_chunks_to_limit(chunks: list[list[str]], limit: int, *, tts_cps: float) -> list[list[str]]:
    merged = [list(chunk) for chunk in chunks]
    limit = max(1, limit)
    while len(merged) > limit:
        merge_index = min(
            range(len(merged) - 1),
            key=lambda index: estimate_audio_s(" ".join(merged[index] + merged[index + 1]), tts_cps),
        )
        merged[merge_index] = merged[merge_index] + merged[merge_index + 1]
        del merged[merge_index + 1]
    return merged


def partition_segments_by_time_weights(
    start_id: int,
    end_id: int,
    weights: list[float],
    by_id: dict[int, FilmMapSegment],
) -> list[tuple[int, int]]:
    segment_ids = [segment_id for segment_id in range(start_id, end_id + 1) if segment_id in by_id]
    if not segment_ids:
        return []
    part_count = max(1, min(len(weights), len(segment_ids)))
    if part_count == 1:
        return [(segment_ids[0], segment_ids[-1])]

    segment_ends = [by_id[segment_id].tc_end for segment_id in segment_ids]
    source_start = by_id[segment_ids[0]].tc_start
    source_end = by_id[segment_ids[-1]].tc_end
    source_duration = max(source_end - source_start, _EPSILON)
    total_weight = max(sum(max(weight, _EPSILON) for weight in weights[:part_count]), _EPSILON)

    spans: list[tuple[int, int]] = []
    cursor_index = 0
    consumed_weight = 0.0
    for part_index in range(part_count):
        remaining_parts = part_count - part_index
        if remaining_parts == 1:
            end_index = len(segment_ids) - 1
        else:
            consumed_weight += max(weights[part_index], _EPSILON)
            target = source_start + source_duration * consumed_weight / total_weight
            min_end_index = cursor_index
            max_end_index = len(segment_ids) - remaining_parts
            end_index = min(
                range(min_end_index, max_end_index + 1),
                key=lambda candidate: _boundary_score(
                    segment_ids,
                    segment_ends,
                    candidate,
                    target,
                    by_id,
                    source_duration,
                ),
            )
        spans.append((segment_ids[cursor_index], segment_ids[end_index]))
        cursor_index = end_index + 1
    return spans

def _boundary_score(
    segment_ids: list[int],
    segment_ends: list[float],
    end_index: int,
    target: float,
    by_id: dict[int, FilmMapSegment],
    source_duration: float,
) -> tuple[float, int]:
    score = abs(segment_ends[end_index] - target)
    next_index = end_index + 1
    if next_index < len(segment_ids):
        next_segment = by_id[segment_ids[next_index]]
        next_duration = next_segment.tc_end - next_segment.tc_start
        if (
            next_duration >= DEFAULT_TARGET_SOURCE_SPAN_S * 0.85
            or next_duration >= source_duration * 0.35
        ):
            score *= 0.35
    return score, end_index

def partition_segments_by_weights(start_id: int, end_id: int, weights: list[float]) -> list[tuple[int, int]]:
    total = end_id - start_id + 1
    part_count = max(1, min(len(weights), total))
    spans: list[tuple[int, int]] = []
    cursor = start_id
    remaining_weight = sum(max(weight, _EPSILON) for weight in weights[:part_count])
    for part_index in range(part_count):
        remaining_segments = end_id - cursor + 1
        remaining_parts = part_count - part_index
        if remaining_parts == 1:
            size = remaining_segments
        else:
            weight = max(weights[part_index], _EPSILON)
            size = round(remaining_segments * weight / max(remaining_weight, _EPSILON))
            size = max(1, min(size, remaining_segments - (remaining_parts - 1)))
        span_end = min(end_id, cursor + size - 1)
        spans.append((cursor, span_end))
        cursor = span_end + 1
        remaining_weight -= max(weights[part_index], _EPSILON)
    return spans
