from __future__ import annotations

from common.schema import Shot


def intersect_duration(shot: Shot, start: float, end: float) -> float:
    return max(0.0, min(shot.tc_end, end) - max(shot.tc_start, start))


def candidates_for_window(shots: list[Shot], start: float, end: float, *, usable_only: bool = True) -> list[Shot]:
    return [
        shot for shot in shots
        if (not usable_only or shot.is_usable) and intersect_duration(shot, start, end) > 0
    ]


def total_candidate_duration(shots: list[Shot], start: float, end: float) -> float:
    return sum(intersect_duration(shot, start, end) for shot in shots)


def widen_until_enough(
    *,
    shots: list[Shot],
    start: float,
    end: float,
    needed_duration: float,
    margin: float,
    max_widen: int,
) -> tuple[float, float, list[Shot], int]:
    current_start = start
    current_end = end
    for widen_count in range(max_widen + 1):
        candidates = candidates_for_window(shots, current_start, current_end)
        if total_candidate_duration(candidates, current_start, current_end) >= needed_duration:
            return current_start, current_end, candidates, widen_count
        current_start = max(0.0, current_start - margin)
        current_end = current_end + margin
    return current_start, current_end, candidates_for_window(shots, current_start, current_end), max_widen
