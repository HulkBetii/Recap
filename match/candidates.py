from __future__ import annotations

from dataclasses import dataclass, field

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
    source_intervals: list[tuple[float, float]] = field(default_factory=list)
    local_expansion_used: bool = False
    local_expansion_gap_s: float = 0.0
    local_expansion_capacity_floor_s: float = 0.0


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


def _shot_ranges_for_intervals(shot: Shot, intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [
        (max(shot.tc_start, start), min(shot.tc_end, end))
        for start, end in intervals
        if min(shot.tc_end, end) > max(shot.tc_start, start) + 1e-6
    ]


def _merge_intervals(intervals: list[tuple[float, float]], *, preserve_order: bool = False) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    ordered_intervals = intervals if preserve_order else sorted(intervals)
    for start, end in ordered_intervals:
        if end <= start + 1e-6:
            continue
        can_merge_with_previous = start <= merged[-1][1] + 1e-6 if merged else False
        if preserve_order and merged:
            can_merge_with_previous = can_merge_with_previous and start >= merged[-1][0] - 1e-6
        if merged and can_merge_with_previous:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _effective_capacity_for_intervals(
    shots: list[Shot],
    intervals: list[tuple[float, float]],
    *,
    max_clip: float,
    min_visual_clip: float,
) -> float:
    total = 0.0
    for shot in shots:
        duration = sum(end - start for start, end in _shot_ranges_for_intervals(shot, intervals))
        if duration <= 1e-6 or duration + 1e-6 < min_visual_clip:
            continue
        total += min(max_clip, duration)
    return total


def is_dark_fallback_candidate(shot: Shot, *, min_visual_clip: float) -> bool:
    if shot.is_usable or not shot.is_story or shot.duration + 1e-6 < min_visual_clip:
        return False
    reasons = set(shot.unusable_reasons)
    if reasons:
        return reasons == {"too_dark"}
    return shot.brightness > 0 and shot.motion_score < LEGACY_TRANSITION_SPIKE_THRESHOLD


def _candidate_plan_for_intervals(
    *,
    candidates: list[Shot],
    dark_candidate_ids: set[int],
    intervals: list[tuple[float, float]],
    needed_duration: float,
    max_clip: float,
    min_visual_clip: float,
    widen_count: int,
    local_expansion_used: bool = False,
    local_expansion_gap_s: float = 0.0,
    local_expansion_capacity_floor_s: float = 0.0,
    preserve_interval_order: bool = False,
) -> CandidatePlan:
    normalized = _merge_intervals(intervals, preserve_order=preserve_interval_order)
    if not normalized:
        raise ValueError("candidate plan requires at least one source interval")
    if preserve_interval_order:
        ordered_candidates = list({shot.index: shot for shot in candidates}.values())
    else:
        ordered_candidates = sorted(candidates, key=lambda item: (item.tc_start, item.tc_end, item.index))
    primary = [shot for shot in ordered_candidates if shot.index not in dark_candidate_ids]
    dark = [shot for shot in ordered_candidates if shot.index in dark_candidate_ids]
    primary_capacity = _effective_capacity_for_intervals(
        primary,
        normalized,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    dark_capacity = _effective_capacity_for_intervals(
        dark,
        normalized,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    total_capacity = primary_capacity + dark_capacity
    return CandidatePlan(
        window_start=min(start for start, _end in normalized),
        window_end=max(end for _start, end in normalized),
        candidates=ordered_candidates,
        dark_candidate_ids=dark_candidate_ids,
        widen_count=widen_count,
        required_duration_s=needed_duration,
        primary_capacity_s=primary_capacity,
        dark_capacity_s=dark_capacity,
        total_capacity_s=total_capacity,
        capacity_exhausted=total_capacity + 1e-6 < needed_duration,
        source_intervals=normalized,
        local_expansion_used=local_expansion_used,
        local_expansion_gap_s=local_expansion_gap_s,
        local_expansion_capacity_floor_s=local_expansion_capacity_floor_s,
    )


def _local_interval_for_shot(
    shot: Shot,
    *,
    start: float,
    end: float,
    max_clip: float,
    needed_duration: float,
) -> tuple[float, float]:
    pad = max(max_clip, needed_duration, 1e-6)
    if shot.tc_end <= start + 1e-6:
        return (max(shot.tc_start, shot.tc_end - pad), shot.tc_end)
    if shot.tc_start >= end - 1e-6:
        return (shot.tc_start, min(shot.tc_end, shot.tc_start + pad))
    return (max(shot.tc_start, start), min(shot.tc_end, end + pad))


def _eligible_local_candidates(
    *,
    shots: list[Shot],
    search_start: float,
    search_end: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
) -> tuple[list[Shot], set[int]]:
    primary = [
        shot for shot in candidates_for_window(shots, search_start, search_end)
        if intersect_duration(shot, search_start, search_end) + 1e-6 >= min_visual_clip
    ]
    dark: list[Shot] = []
    if allow_dark_fallback:
        dark = [
            shot for shot in shots
            if is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)
            and intersect_duration(shot, search_start, search_end) + 1e-6 >= min_visual_clip
        ]
    ordered = sorted(primary + dark, key=lambda item: (item.tc_start, item.tc_end, item.index))
    unique: dict[int, Shot] = {shot.index: shot for shot in ordered}
    return list(unique.values()), {shot.index for shot in dark}


def _short_source_local_expansion(
    *,
    shots: list[Shot],
    start: float,
    end: float,
    needed_duration: float,
    margin: float,
    max_clip: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
) -> CandidatePlan | None:
    if margin <= 0 or needed_duration <= 1e-6:
        return None
    search_start = max(0.0, start - margin)
    search_end = end + margin
    local_gap_limit = max(min_visual_clip, min(6.0, max(2.0, margin / 3.0)))
    capacity_floor = max(min_visual_clip, needed_duration * 0.70)
    eligible, dark_ids = _eligible_local_candidates(
        shots=shots,
        search_start=search_start,
        search_end=search_end,
        min_visual_clip=min_visual_clip,
        allow_dark_fallback=allow_dark_fallback,
    )
    if not eligible:
        return None

    selected: list[Shot] = []
    selected_ids: set[int] = set()
    cluster_start = start
    cluster_end = end
    max_gap = 0.0

    def add_candidate(candidate: Shot, gap: float) -> None:
        nonlocal cluster_start, cluster_end, max_gap
        selected.append(candidate)
        selected_ids.add(candidate.index)
        cluster_start = min(cluster_start, candidate.tc_start)
        cluster_end = max(cluster_end, candidate.tc_end)
        max_gap = max(max_gap, gap)

    for candidate in eligible:
        if candidate.tc_start < end - 1e-6 and candidate.tc_end > start + 1e-6:
            add_candidate(candidate, 0.0)

    while True:
        intervals = [
            _local_interval_for_shot(
                candidate,
                start=start,
                end=end,
                max_clip=max_clip,
                needed_duration=needed_duration,
            )
            for candidate in selected
        ]
        if intervals:
            plan = _candidate_plan_for_intervals(
                candidates=selected,
                dark_candidate_ids=dark_ids & selected_ids,
                intervals=intervals,
                needed_duration=needed_duration,
                max_clip=max_clip,
                min_visual_clip=min_visual_clip,
                widen_count=0,
                local_expansion_used=True,
                local_expansion_gap_s=max_gap,
                local_expansion_capacity_floor_s=capacity_floor,
                preserve_interval_order=True,
            )
            if not plan.capacity_exhausted or plan.total_capacity_s + 1e-6 >= capacity_floor:
                return plan

        adjacent: list[tuple[float, int, Shot]] = []
        for candidate in eligible:
            if candidate.index in selected_ids:
                continue
            if candidate.tc_end <= cluster_start + 1e-6:
                gap = max(0.0, cluster_start - candidate.tc_end)
                side = 1
            elif candidate.tc_start >= cluster_end - 1e-6:
                gap = max(0.0, candidate.tc_start - cluster_end)
                side = 0
            else:
                gap = 0.0
                side = 1 if candidate.tc_start < start else 0
            if gap <= local_gap_limit + 1e-6:
                adjacent.append((gap, side, candidate))
        if not adjacent:
            return None
        adjacent.sort(key=lambda item: (item[0], item[1], item[2].tc_start, item[2].index))
        gap, _side, candidate = adjacent[0]
        add_candidate(candidate, gap)


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
            source_intervals=[(current_start, current_end)],
        )
        if not final_plan.capacity_exhausted:
            return final_plan
        if widen_count == 0:
            local_plan = _short_source_local_expansion(
                shots=shots,
                start=start,
                end=end,
                needed_duration=needed_duration,
                margin=margin,
                max_clip=max_clip,
                min_visual_clip=min_visual_clip,
                allow_dark_fallback=allow_dark_fallback,
            )
            if local_plan is not None:
                return local_plan
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
