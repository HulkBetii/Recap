from __future__ import annotations

from common.schema import SilentGap, TranslatedSegment


def detect_silent_gaps(
    segments: list[TranslatedSegment],
    duration: float,
    threshold: float,
) -> list[SilentGap]:
    ordered = sorted(segments, key=lambda item: (item.tc_start, item.tc_end))
    candidates: list[tuple[float, float]] = []
    cursor = 0.0
    for segment in ordered:
        start = max(0.0, min(segment.tc_start, duration))
        end = max(0.0, min(segment.tc_end, duration))
        if start - cursor > threshold:
            candidates.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > threshold:
        candidates.append((cursor, duration))
    return [SilentGap(id=index, tc_start=start, tc_end=end) for index, (start, end) in enumerate(candidates)]


def select_gaps_for_vision(gaps: list[SilentGap], max_frames: int) -> list[SilentGap]:
    if max_frames <= 0:
        return []
    if len(gaps) <= max_frames:
        return gaps
    selected_ids = {
        gap.id
        for gap in sorted(gaps, key=lambda item: item.duration, reverse=True)[:max_frames]
    }
    return [gap for gap in gaps if gap.id in selected_ids]
