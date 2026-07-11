from __future__ import annotations

from common.schema import EdlPlacement, Shot, validate_edl


def validate_timeline(placements: list[EdlPlacement], total_duration: float) -> list[EdlPlacement]:
    return validate_edl(placements, total_duration)


def average_clip_len(placements: list[EdlPlacement]) -> float:
    if not placements:
        return 0.0
    return sum(item.tl_end - item.tl_start for item in placements) / len(placements)


def validate_source_bounds(placements: list[EdlPlacement], shots: list[Shot]) -> list[EdlPlacement]:
    shots_by_index = {shot.index: shot for shot in shots}
    for placement in placements:
        shot = shots_by_index.get(placement.shot_index)
        if shot is None:
            raise ValueError(f"EDL placement references unknown shot #{placement.shot_index}")
        if placement.src_in < shot.tc_start - 1e-3 or placement.src_out > shot.tc_end + 1e-3:
            raise ValueError(
                f"EDL placement beat #{placement.beat_id}/shot #{placement.shot_index} exceeds shot source bounds"
            )
    return placements
