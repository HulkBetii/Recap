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


def split_long_gaps(gaps: list[SilentGap], max_gap_s: float) -> list[SilentGap]:
    if max_gap_s <= 0:
        return [gap.model_copy(update={"id": index}) for index, gap in enumerate(gaps)]
    split: list[SilentGap] = []
    for gap in gaps:
        duration = gap.tc_end - gap.tc_start
        if duration <= max_gap_s:
            split.append(SilentGap(id=0, tc_start=gap.tc_start, tc_end=gap.tc_end))
            continue
        cursor = gap.tc_start
        while cursor < gap.tc_end - 1e-6:
            end = min(gap.tc_end, cursor + max_gap_s)
            if end > cursor:
                split.append(SilentGap(id=0, tc_start=round(cursor, 3), tc_end=round(end, 3)))
            cursor = end
    return [gap.model_copy(update={"id": index}) for index, gap in enumerate(split)]

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
