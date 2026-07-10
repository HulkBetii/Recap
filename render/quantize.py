from __future__ import annotations

from dataclasses import dataclass

from common.schema import EdlPlacement

@dataclass(frozen=True)
class FramePlacement:
    placement: EdlPlacement
    index: int
    f_start: int
    f_end: int
    frame_count: int
    duration_s: float

class QuantizeError(ValueError):
    pass

def quantize_placements(placements: list[EdlPlacement], fps: float) -> list[FramePlacement]:
    if fps <= 0:
        raise QuantizeError("fps must be greater than zero")
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id, item.shot_index))
    frame_placements: list[FramePlacement] = []
    previous_end: int | None = None
    for index, placement in enumerate(ordered):
        f_start = round(placement.tl_start * fps)
        f_end = round(placement.tl_end * fps)
        if previous_end is not None:
            if abs(f_start - previous_end) > 1:
                raise QuantizeError(f"timeline has frame gap or overlap before placement #{index}")
            f_start = previous_end
        if f_end <= f_start:
            # Timeline fragments shorter than one output frame cannot be rendered as standalone clips.
            # Drop them here; the following placement starts from previous_end and absorbs the frame time.
            continue
        frame_count = f_end - f_start
        frame_placements.append(FramePlacement(
            placement=placement,
            index=index,
            f_start=f_start,
            f_end=f_end,
            frame_count=frame_count,
            duration_s=frame_count / fps,
        ))
        previous_end = f_end
    if not frame_placements and ordered:
        raise QuantizeError("all placements have zero frames after quantization")
    if frame_placements:
        total_frames = round(ordered[-1].tl_end * fps)
        if frame_placements[-1].f_end != total_frames:
            raise QuantizeError("quantized total frames do not match timeline duration")
    return frame_placements
