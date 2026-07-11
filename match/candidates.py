from __future__ import annotations

from dataclasses import dataclass

from common.schema import Shot


LEGACY_TRANSITION_SPIKE_THRESHOLD = 0.92


@dataclass(frozen=True)
class CandidatePlan:
    window_start: float
    window_end: float
    candidates: list[Shot]
    dark_candidate_ids: set[int]
    widen_count: int
    required_duration_s: float
    primary_capacity_s: float
    dark_capacity_s: float
    total_capacity_s: float
    capacity_exhausted: bool


def intersect_duration(shot: Shot, start: float, end: float) -> float:
    return max(0.0, min(shot.tc_end, end) - max(shot.tc_start, start))


def candidates_for_window(shots: list[Shot], start: float, end: float, *, usable_only: bool = True) -> list[Shot]:
    return [
        shot for shot in shots
        if (not usable_only or shot.is_usable) and intersect_duration(shot, start, end) > 0
    ]


def total_candidate_duration(shots: list[Shot], start: float, end: float) -> float:
    return sum(intersect_duration(shot, start, end) for shot in shots)


def effective_candidate_capacity(
    shots: list[Shot],
    start: float,
    end: float,
    *,
    max_clip: float,
    min_visual_clip: float,
) -> float:
    return sum(
        min(max_clip, duration)
        for shot in shots
        if (duration := intersect_duration(shot, start, end)) > 1e-6
        and duration + 1e-6 >= min_visual_clip
    )


def is_dark_fallback_candidate(shot: Shot, *, min_visual_clip: float) -> bool:
    if shot.is_usable or not shot.is_story or shot.duration + 1e-6 < min_visual_clip:
        return False
    reasons = set(shot.unusable_reasons)
    if reasons:
        return reasons == {"too_dark"}
    return shot.brightness > 0 and shot.motion_score < LEGACY_TRANSITION_SPIKE_THRESHOLD


def plan_candidates(
    *,
    shots: list[Shot],
    start: float,
    end: float,
    needed_duration: float,
    margin: float,
    max_widen: int,
    max_clip: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
) -> CandidatePlan:
    final_plan: CandidatePlan | None = None
    for widen_count in range(max_widen + 1):
        current_start = max(0.0, start - margin * widen_count)
        current_end = end + margin * widen_count
        primary = [
            shot for shot in candidates_for_window(shots, current_start, current_end)
            if intersect_duration(shot, current_start, current_end) + 1e-6 >= min_visual_clip
        ]
        primary_capacity = effective_candidate_capacity(
            primary,
            current_start,
            current_end,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
        )
        dark: list[Shot] = []
        if allow_dark_fallback and primary_capacity + 1e-6 < needed_duration:
            dark = [
                shot for shot in shots
                if is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)
                and (intersection := intersect_duration(shot, current_start, current_end)) > 1e-6
                and intersection + 1e-6 >= min_visual_clip
            ]
        dark_capacity = effective_candidate_capacity(
            dark,
            current_start,
            current_end,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
        )
        total_capacity = primary_capacity + dark_capacity
        final_plan = CandidatePlan(
            window_start=current_start,
            window_end=current_end,
            candidates=primary + dark,
            dark_candidate_ids={shot.index for shot in dark},
            widen_count=widen_count,
            required_duration_s=needed_duration,
            primary_capacity_s=primary_capacity,
            dark_capacity_s=dark_capacity,
            total_capacity_s=total_capacity,
            capacity_exhausted=total_capacity + 1e-6 < needed_duration,
        )
        if not final_plan.capacity_exhausted:
            return final_plan
    if final_plan is None:
        raise ValueError("candidate planner did not evaluate a source window")
    return final_plan


def widen_until_enough(
    *,
    shots: list[Shot],
    start: float,
    end: float,
    needed_duration: float,
    margin: float,
    max_widen: int,
) -> tuple[float, float, list[Shot], int]:
    for widen_count in range(max_widen + 1):
        current_start = max(0.0, start - margin * widen_count)
        current_end = end + margin * widen_count
        candidates = candidates_for_window(shots, current_start, current_end)
        if total_candidate_duration(candidates, current_start, current_end) >= needed_duration:
            return current_start, current_end, candidates, widen_count
    current_start = max(0.0, start - margin * max_widen)
    current_end = end + margin * max_widen
    return current_start, current_end, candidates_for_window(shots, current_start, current_end), max_widen
