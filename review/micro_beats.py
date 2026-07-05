from __future__ import annotations

import re
from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat

_SENTENCE_RE = re.compile(r"(?<=[.!????])\s+")

@dataclass(frozen=True)
class MicroBeatReport:
    n_split_beats: int
    split_beat_ids: list[int]
    warnings: list[str]


def split_long_beats(
    beats: list[ReviewBeat],
    film_map: list[FilmMapSegment],
    *,
    max_audio_s: float,
    target_audio_s: float,
    tts_cps: float,
    enabled: bool,
) -> tuple[list[ReviewBeat], MicroBeatReport]:
    if not enabled or max_audio_s <= 0 or target_audio_s <= 0 or tts_cps <= 0:
        return beats, MicroBeatReport(0, [], [])
    by_id = {segment.id: segment for segment in film_map}
    output: list[ReviewBeat] = []
    split_ids: list[int] = []
    warnings: list[str] = []
    for beat in beats:
        estimated_s = len(beat.narration) / tts_cps
        segment_count = beat.to_seg_id - beat.from_seg_id + 1
        sentences = split_sentences(beat.narration)
        if beat.is_hook or estimated_s <= max_audio_s or segment_count < 2 or len(sentences) < 2:
            output.append(beat)
            continue
        part_count = max(2, min(3, len(sentences), int(round(estimated_s / target_audio_s)) or 2, segment_count))
        parts = partition_sentences(sentences, part_count)
        spans = partition_segments(beat.from_seg_id, beat.to_seg_id, len(parts))
        if len(parts) != len(spans):
            output.append(beat)
            continue
        split_ids.append(beat.beat_id)
        warnings.append(f"split beat {beat.beat_id} into {len(parts)} micro-beats")
        for text, (from_seg_id, to_seg_id) in zip(parts, spans):
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
    return normalized, MicroBeatReport(len(split_ids), split_ids, warnings)


def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return []
    parts = [part.strip() for part in _SENTENCE_RE.split(normalized) if part.strip()]
    return parts or [normalized]


def partition_sentences(sentences: list[str], part_count: int) -> list[str]:
    part_count = max(1, min(part_count, len(sentences)))
    buckets = [[] for _ in range(part_count)]
    for index, sentence in enumerate(sentences):
        bucket_index = min(part_count - 1, int(index * part_count / len(sentences)))
        buckets[bucket_index].append(sentence)
    return [" ".join(bucket).strip() for bucket in buckets if bucket]


def partition_segments(start_id: int, end_id: int, part_count: int) -> list[tuple[int, int]]:
    total = end_id - start_id + 1
    part_count = max(1, min(part_count, total))
    spans: list[tuple[int, int]] = []
    cursor = start_id
    for part_index in range(part_count):
        remaining_segments = end_id - cursor + 1
        remaining_parts = part_count - part_index
        size = max(1, round(remaining_segments / remaining_parts))
        span_end = min(end_id, cursor + size - 1)
        spans.append((cursor, span_end))
        cursor = span_end + 1
    return spans
