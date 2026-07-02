from __future__ import annotations

from common.schema import EdlPlacement, validate_edl


def validate_timeline(placements: list[EdlPlacement], total_duration: float) -> list[EdlPlacement]:
    return validate_edl(placements, total_duration)


def average_clip_len(placements: list[EdlPlacement]) -> float:
    if not placements:
        return 0.0
    return sum(item.tl_end - item.tl_start for item in placements) / len(placements)
