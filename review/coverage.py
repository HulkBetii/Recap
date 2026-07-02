from __future__ import annotations

from common.schema import ReviewBeat


def coverage_ratio(beats: list[ReviewBeat], total_segments: int) -> float:
    if total_segments <= 0:
        return 0.0
    covered: set[int] = set()
    for beat in beats:
        if beat.is_hook:
            continue
        covered.update(range(beat.from_seg_id, beat.to_seg_id + 1))
    return min(1.0, len(covered) / total_segments)


def non_hook_monotonic(beats: list[ReviewBeat]) -> bool:
    starts = [beat.src_tc_start for beat in beats if not beat.is_hook]
    return starts == sorted(starts)
