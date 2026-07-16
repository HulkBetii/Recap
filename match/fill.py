from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil, floor

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.candidates import plan_candidates
from match.scoring import ScoringWeights, rank_shots, score_shot


@dataclass
class Fragment:
    src: str
    src_in: float
    src_out: float
    beat_id: int
    shot_index: int
    reused: bool
    speed: float = 1.0

    @property
    def duration(self) -> float:
        return self.src_out - self.src_in


@dataclass
class FillResult:
    fragments: list[Fragment]
    widened: bool
    reused_count: int
    speedfit_count: int
    warnings: list[str]
    candidate_shot_ids: list[int]
    window_start: float
    window_end: float
    source_cursor_start: float
    widen_count: int
    required_duration_s: float
    primary_capacity_s: float
    dark_capacity_s: float
    total_capacity_s: float
    capacity_exhausted: bool
    dark_candidate_ids: list[int]
    dark_selected_ids: list[int]
    unused_source_reuse_count: int
    overlapping_repeat_count: int
    source_intervals: list[tuple[float, float]]
    source_interval_weights: list[float]
    local_expansion_used: bool
    local_expansion_gap_s: float
    local_expansion_capacity_floor_s: float


def prefer_unused_global_candidates(
    available: list[Shot],
    *,
    reuse_counts: dict[int, int],
    source_intervals: list[tuple[float, float]],
    remaining_duration: float,
    max_clip: float,
    min_visual_clip: float,
) -> tuple[list[Shot], bool]:
    if remaining_duration <= 1e-6:
        return available, False
    unused = [shot for shot in available if reuse_counts.get(shot.index, 0) == 0]
    if not unused or len(unused) == len(available):
        return available, False
    unused_capacity = candidate_capacity(
        unused,
        source_intervals,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    if unused_capacity + 1e-6 >= remaining_duration:
        return unused, True
    return available, False


def fill_beat(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    shots: list[Shot],
    reuse_counts: dict[int, int],
    weights: ScoringWeights,
    min_clip: float,
    max_clip: float,
    widen_margin: float,
    max_widen: int,
    allow_repeat: bool,
    allow_speedfit: bool,
    semantic_scores: dict[tuple[int, int], float] | None = None,
    visual_scores: dict[tuple[int, int], float] | None = None,
    max_repeat_per_beat: int = 2,
    max_repeat_ratio_per_beat: float = 0.35,
    min_repeat_alternative_score_ratio: float = 0.75,
    adjacent_shot_repeat_penalty: float = 0.50,
    ordered_fill: bool = False,
    ordered_fill_by_audio_progress: bool = True,
    match_strategy: str = "hybrid",
    chronology_weight: float = 0.70,
    max_source_drift_s: float = 12.0,
    source_start_override: float | None = None,
    min_visual_clip: float = 0.0,
    strict_ordered_fill: bool = False,
    allow_dark_fallback: bool = True,
    visual_priority: bool = False,
    candidate_filter_ids: set[int] | None = None,
    dark_candidate_ids: set[int] | None = None,
    source_intervals: list[tuple[float, float]] | None = None,
) -> FillResult:
    warnings: list[str] = []
    effective_start = max(beat.src_tc_start, source_start_override) if source_start_override is not None else beat.src_tc_start
    candidate_plan = plan_candidates(
        shots=shots,
        start=effective_start,
        end=beat.src_tc_end,
        needed_duration=timing.duration,
        margin=widen_margin,
        max_widen=max_widen,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
        allow_dark_fallback=allow_dark_fallback,
    )
    active_intervals = normalize_source_intervals(
        source_intervals or candidate_plan.source_intervals or [(candidate_plan.window_start, candidate_plan.window_end)],
        window_start=candidate_plan.window_start,
        window_end=candidate_plan.window_end,
        preserve_order=bool(candidate_plan.local_expansion_used and source_intervals is None),
    )
    window_start = min(start for start, _end in active_intervals)
    window_end = max(end for _start, end in active_intervals)
    if candidate_filter_ids is None:
        candidates = candidate_plan.candidates
        effective_dark_ids = set(candidate_plan.dark_candidate_ids)
    else:
        allowed_ids = set(candidate_filter_ids)
        effective_dark_ids = set(dark_candidate_ids or set()) & allowed_ids
        candidates = [
            shot
            for shot in shots
            if shot.index in allowed_ids
            and (shot.is_usable or shot.index in effective_dark_ids)
            and shot_source_ranges(shot, active_intervals)
        ]
    primary_capacity = candidate_capacity(
        [shot for shot in candidates if shot.index not in effective_dark_ids],
        active_intervals,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    dark_capacity = candidate_capacity(
        [shot for shot in candidates if shot.index in effective_dark_ids],
        active_intervals,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
    )
    total_capacity = primary_capacity + dark_capacity
    capacity_exhausted = total_capacity + 1e-6 < timing.duration
    source_interval_weights = [
        candidate_capacity(candidates, [interval], max_clip=max_clip, min_visual_clip=min_visual_clip)
        for interval in active_intervals
    ]
    widen_count = candidate_plan.widen_count
    if widen_count > 0:
        warnings.append(f"beat {beat.beat_id} widened source window {widen_count} time(s)")
    if candidate_plan.local_expansion_used:
        warnings.append(
            f"beat {beat.beat_id} used local same-scene expansion "
            f"capacity={total_capacity:.3f}/{timing.duration:.3f}s"
        )
    if capacity_exhausted:
        warnings.append(
            f"beat {beat.beat_id} candidate capacity exhausted "
            f"{total_capacity:.3f}/{timing.duration:.3f}s"
        )
    fragments: list[Fragment] = []
    remaining = timing.duration
    used_in_beat: set[int] = set()
    reuse_count = 0
    speedfit_count = 0
    repeat_by_shot: dict[int, int] = {}
    used_ranges_by_shot: dict[int, list[tuple[float, float]]] = {}
    dark_selected_ids: set[int] = set()
    unused_source_reuse_count = 0
    overlapping_repeat_count = 0
    previous_shot_index: int | None = None
    source_cursor = source_position_for_progress(active_intervals, 0.0, weights=source_interval_weights)
    source_anchor = source_cursor

    while remaining > 1e-6:
        if min_visual_clip > 0 and remaining < min_visual_clip - 1e-6 and fragments:
            break
        available = [shot for shot in candidates if shot.index not in used_in_beat]
        if min_visual_clip > 0:
            long_enough = []
            for shot in available:
                ranges = shot_source_ranges(shot, active_intervals)
                if any(end - start + 1e-6 >= min_visual_clip for start, end in ranges):
                    long_enough.append(shot)
                elif any(
                    ordered_fill_short_bridge_allowed(
                        selected_range=(start, end),
                        source_cursor=source_cursor,
                        source_intervals=active_intervals,
                        available_shots=available,
                        min_visual_clip=min_visual_clip,
                        strict_ordered_fill=strict_ordered_fill,
                        match_strategy=match_strategy,
                    )
                    for start, end in ranges
                ):
                    long_enough.append(shot)
            available = long_enough
        if not available:
            break
        if strict_ordered_fill:
            source_cursor = guard_source_cursor_no_early_jump(
                source_cursor,
                fragments=fragments,
                available_shots=available,
                source_intervals=active_intervals,
                min_visual_clip=min_visual_clip,
                max_source_drift_s=max_source_drift_s,
            )
        available, reused_pref = prefer_unused_global_candidates(
            available,
            reuse_counts=reuse_counts,
            source_intervals=active_intervals,
            remaining_duration=remaining,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
        )
        if reused_pref:
            warnings.append(f"beat {beat.beat_id} preferred unused global shots while capacity allowed")
        ranked = rank_shots(available, reuse_counts, weights, semantic_scores, visual_scores, beat.beat_id)
        if ordered_fill or match_strategy in {"chronological", "hybrid"}:
            ranked = rank_for_ordered_fill(
                ranked,
                reuse_counts,
                weights,
                semantic_scores or {},
                visual_scores or {},
                beat.beat_id,
                source_cursor,
                match_strategy=match_strategy,
                chronology_weight=chronology_weight,
                max_source_drift_s=max_source_drift_s,
                strict_ordered_fill=strict_ordered_fill,
                visual_priority=visual_priority,
            )
        shot = choose_diverse_shot(
            ranked,
            reuse_counts,
            weights,
            semantic_scores or {},
            visual_scores or {},
            beat.beat_id,
            previous_shot_index,
            min_repeat_alternative_score_ratio,
            adjacent_shot_repeat_penalty,
            source_cursor=source_cursor,
            max_source_drift_s=max_source_drift_s,
            enforce_chronology_tier=ordered_fill or match_strategy == "chronological",
        )
        selected_range = choose_source_range(shot_source_ranges(shot, active_intervals), source_cursor)
        if selected_range is None:
            used_in_beat.add(shot.index)
            continue
        src_start, src_end = selected_range
        if match_strategy == "chronological":
            src_start = ordered_fill_source_start(
                src_start,
                src_end,
                source_cursor,
                strict_ordered_fill=strict_ordered_fill,
                min_visual_clip=min_visual_clip,
            )
        usable_len = max(0.0, src_end - src_start)
        short_bridge_allowed = (
            usable_len > 0
            and min_visual_clip > 0
            and usable_len + 1e-6 < min_visual_clip
            and ordered_fill_short_bridge_allowed(
                selected_range=(src_start, src_end),
                source_cursor=source_cursor,
                source_intervals=active_intervals,
                available_shots=available,
                min_visual_clip=min_visual_clip,
                strict_ordered_fill=strict_ordered_fill,
                match_strategy=match_strategy,
            )
        )
        if usable_len <= 0 or (min_visual_clip > 0 and usable_len + 1e-6 < min_visual_clip and not short_bridge_allowed):
            used_in_beat.add(shot.index)
            continue
        clip_len = min(max_clip, usable_len, remaining)
        if clip_len < min_clip and remaining > min_clip and usable_len >= min_clip:
            clip_len = min(min_clip, remaining)
        remainder = remaining - clip_len
        if min_visual_clip > 0 and 1e-6 < remainder < min_visual_clip:
            if remaining <= max_clip and usable_len >= remaining:
                clip_len = remaining
            else:
                adjustment = min_visual_clip - remainder
                if clip_len - adjustment >= min_visual_clip:
                    clip_len -= adjustment
        if clip_len <= 0:
            break
        fragment = Fragment(
            src=shot.src,
            src_in=round(src_start, 3),
            src_out=round(src_start + clip_len, 3),
            beat_id=beat.beat_id,
            shot_index=shot.index,
            reused=reuse_counts.get(shot.index, 0) > 0,
        )
        fragments.append(fragment)
        used_ranges_by_shot.setdefault(shot.index, []).append((fragment.src_in, fragment.src_out))
        if shot.index in effective_dark_ids:
            dark_selected_ids.add(shot.index)
        if reuse_counts.get(shot.index, 0) > 0:
            reuse_count += 1
            repeat_by_shot[shot.index] = repeat_by_shot.get(shot.index, 0) + 1
        reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
        previous_shot_index = shot.index
        used_in_beat.add(shot.index)
        remaining = round(remaining - clip_len, 6)
        if ordered_fill_by_audio_progress and timing.duration > 0:
            progress = min(1.0, max(0.0, (timing.duration - remaining) / timing.duration))
            source_cursor = source_position_for_progress(active_intervals, progress, weights=source_interval_weights)

    if remaining > 0.02:
        if allow_repeat and candidates:
            warnings.append(f"beat {beat.beat_id} required controlled repeat fallback")
            while remaining > 1e-6:
                if min_visual_clip > 0 and remaining < min_visual_clip - 1e-6 and fragments:
                    break
                ranked_repeat = [shot for shot in rank_shots(candidates, reuse_counts, weights, semantic_scores, visual_scores, beat.beat_id) if repeat_by_shot.get(shot.index, 0) < max_repeat_per_beat]
                if ordered_fill or match_strategy in {"chronological", "hybrid"}:
                    ranked_repeat = rank_for_ordered_fill(
                        ranked_repeat,
                        reuse_counts,
                        weights,
                        semantic_scores or {},
                        visual_scores or {},
                        beat.beat_id,
                        source_cursor,
                        match_strategy=match_strategy,
                        chronology_weight=chronology_weight,
                        max_source_drift_s=max_source_drift_s,
                        strict_ordered_fill=strict_ordered_fill,
                        visual_priority=visual_priority,
                    )
                if not ranked_repeat:
                    ranked_repeat = rank_shots(candidates, reuse_counts, weights, semantic_scores, visual_scores, beat.beat_id)
                    if ordered_fill or match_strategy in {"chronological", "hybrid"}:
                        ranked_repeat = rank_for_ordered_fill(
                            ranked_repeat,
                            reuse_counts,
                            weights,
                            semantic_scores or {},
                            visual_scores or {},
                            beat.beat_id,
                            source_cursor,
                            match_strategy=match_strategy,
                            chronology_weight=chronology_weight,
                            max_source_drift_s=max_source_drift_s,
                            strict_ordered_fill=strict_ordered_fill,
                            visual_priority=visual_priority,
                        )
                    warnings.append(f"beat {beat.beat_id} exceeded repeat cap during fallback")
                ranked_repeat, reused_pref = prefer_unused_global_candidates(
                    ranked_repeat,
                    reuse_counts=reuse_counts,
                    source_intervals=active_intervals,
                    remaining_duration=remaining,
                    max_clip=max_clip,
                    min_visual_clip=min_visual_clip,
                )
                if reused_pref:
                    warnings.append(f"beat {beat.beat_id} preferred unused global repeat candidates while capacity allowed")
                unused_ranked = [
                    shot for shot in ranked_repeat
                    if any(
                        end - start + 1e-6 >= min_visual_clip
                        for start, end in uncovered_source_ranges(
                            shot,
                            window_start,
                            window_end,
                            used_ranges_by_shot.get(shot.index, []),
                            source_intervals=active_intervals,
                        )
                    )
                ]
                repeat_pool = unused_ranked or ranked_repeat
                repeat_pool = avoid_adjacent_repeat_in_tier(
                    repeat_pool,
                    previous_shot_index=previous_shot_index,
                    source_cursor=source_cursor,
                    max_source_drift_s=max_source_drift_s,
                )
                shot = choose_diverse_shot(
                    repeat_pool,
                    reuse_counts,
                    weights,
                    semantic_scores or {},
                    visual_scores or {},
                    beat.beat_id,
                    previous_shot_index,
                    min_repeat_alternative_score_ratio,
                    adjacent_shot_repeat_penalty,
                    source_cursor=source_cursor,
                    max_source_drift_s=max_source_drift_s,
                    enforce_chronology_tier=ordered_fill or match_strategy == "chronological",
                )
                used_ranges = used_ranges_by_shot.get(shot.index, [])
                uncovered = uncovered_source_ranges(
                    shot,
                    window_start,
                    window_end,
                    used_ranges,
                    source_intervals=active_intervals,
                )
                selected_range = choose_uncovered_range(uncovered, source_cursor) if unused_ranked else None
                if selected_range is not None:
                    src_start, range_end = selected_range
                    usable_len = range_end - src_start
                    unused_source_reuse_count += 1
                else:
                    overlap_range = least_overlap_range(
                        shot,
                        window_start,
                        window_end,
                        min(max_clip, remaining),
                        used_ranges,
                        source_cursor,
                        source_intervals=active_intervals,
                    )
                    if overlap_range is None:
                        break
                    src_start, range_end = overlap_range
                    usable_len = range_end - src_start
                    overlapping_repeat_count += 1
                clip_len = min(max_clip, remaining, usable_len)
                remainder = remaining - clip_len
                if min_visual_clip > 0 and 1e-6 < remainder < min_visual_clip:
                    adjustment = min_visual_clip - remainder
                    if clip_len - adjustment >= min_visual_clip:
                        clip_len -= adjustment
                if clip_len <= 0 or (min_visual_clip > 0 and clip_len + 1e-6 < min_visual_clip):
                    break
                fragment = Fragment(
                    src=shot.src,
                    src_in=round(src_start, 3),
                    src_out=round(src_start + clip_len, 3),
                    beat_id=beat.beat_id,
                    shot_index=shot.index,
                    reused=True,
                )
                fragments.append(fragment)
                used_ranges_by_shot.setdefault(shot.index, []).append((fragment.src_in, fragment.src_out))
                if shot.index in effective_dark_ids:
                    dark_selected_ids.add(shot.index)
                reuse_count += 1
                repeat_by_shot[shot.index] = repeat_by_shot.get(shot.index, 0) + 1
                reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
                previous_shot_index = shot.index
                remaining = round(remaining - clip_len, 6)
                if ordered_fill_by_audio_progress and timing.duration > 0:
                    progress = min(1.0, max(0.0, (timing.duration - remaining) / timing.duration))
                    source_cursor = source_position_for_progress(active_intervals, progress, weights=source_interval_weights)
        elif allow_speedfit and fragments:
            warnings.append(f"beat {beat.beat_id} would require speedfit; not applied to existing fragments")
            speedfit_count += 1
        else:
            warnings.append(f"beat {beat.beat_id} could not fill {remaining:.3f}s")

    total_fragments = len(fragments)
    repeat_ratio = (reuse_count / total_fragments) if total_fragments else 0.0
    if total_fragments == 0:
        warnings.append(f"beat {beat.beat_id} empty beat placements")
    if total_fragments and repeat_ratio > max_repeat_ratio_per_beat:
        warnings.append(f"beat {beat.beat_id} high repeat ratio {repeat_ratio:.3f} > {max_repeat_ratio_per_beat:.3f}")
    if dark_selected_ids:
        warnings.append(f"beat {beat.beat_id} used dark local fallback {len(dark_selected_ids)} shot(s)")
    if unused_source_reuse_count:
        warnings.append(f"beat {beat.beat_id} reused unused source ranges {unused_source_reuse_count} time(s)")
    if overlapping_repeat_count:
        warnings.append(f"beat {beat.beat_id} used overlapping source repeat {overlapping_repeat_count} time(s)")

    ordered_fragments = fragments if candidate_plan.local_expansion_used else sorted(
        fragments,
        key=lambda item: (item.src_in, item.src_out),
    )
    fragments = trim_fragments_to_duration(
        ordered_fragments,
        timing.duration,
        min_visual_clip=min_visual_clip,
    )
    return FillResult(
        fragments=fragments,
        widened=widen_count > 0 or candidate_plan.local_expansion_used,
        reused_count=reuse_count,
        speedfit_count=speedfit_count,
        warnings=warnings,
        candidate_shot_ids=[shot.index for shot in candidates],
        window_start=window_start,
        window_end=window_end,
        source_cursor_start=source_anchor,
        widen_count=widen_count,
        required_duration_s=timing.duration,
        primary_capacity_s=primary_capacity,
        dark_capacity_s=dark_capacity,
        total_capacity_s=total_capacity,
        capacity_exhausted=capacity_exhausted,
        dark_candidate_ids=sorted(effective_dark_ids),
        dark_selected_ids=sorted(dark_selected_ids),
        unused_source_reuse_count=unused_source_reuse_count,
        overlapping_repeat_count=overlapping_repeat_count,
        source_intervals=active_intervals,
        source_interval_weights=source_interval_weights,
        local_expansion_used=candidate_plan.local_expansion_used,
        local_expansion_gap_s=candidate_plan.local_expansion_gap_s,
        local_expansion_capacity_floor_s=candidate_plan.local_expansion_capacity_floor_s,
    )


def normalize_source_intervals(
    intervals: list[tuple[float, float]],
    *,
    window_start: float,
    window_end: float,
    preserve_order: bool = False,
) -> list[tuple[float, float]]:
    normalized: list[tuple[float, float]] = []
    ordered_intervals = intervals if preserve_order else sorted(intervals)
    for start, end in ordered_intervals:
        clipped_start = max(window_start, start)
        clipped_end = min(window_end, end)
        if clipped_end <= clipped_start + 1e-6:
            continue
        can_merge = bool(normalized) and clipped_start <= normalized[-1][1] + 1e-6
        if preserve_order and normalized:
            can_merge = can_merge and clipped_start >= normalized[-1][0] - 1e-6
        if can_merge:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], clipped_end))
        else:
            normalized.append((clipped_start, clipped_end))
    return normalized or [(window_start, window_end)]


def shot_source_ranges(shot: Shot, source_intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [
        (max(shot.tc_start, start), min(shot.tc_end, end))
        for start, end in source_intervals
        if min(shot.tc_end, end) > max(shot.tc_start, start) + 1e-6
    ]


def candidate_capacity(
    shots: list[Shot],
    source_intervals: list[tuple[float, float]],
    *,
    max_clip: float,
    min_visual_clip: float,
) -> float:
    total = 0.0
    for shot in shots:
        duration = sum(end - start for start, end in shot_source_ranges(shot, source_intervals))
        if duration <= 1e-6 or duration + 1e-6 < min_visual_clip:
            continue
        total += min(max_clip, duration)
    return total


def source_position_for_progress(
    intervals: list[tuple[float, float]],
    progress: float,
    *,
    weights: list[float] | None = None,
) -> float:
    if not intervals:
        raise ValueError("source intervals cannot be empty")
    clamped = min(1.0, max(0.0, progress))
    durations = [max(0.0, end - start) for start, end in intervals]
    effective_weights = weights if weights is not None and len(weights) == len(intervals) and sum(weights) > 1e-6 else durations
    total = sum(effective_weights)
    if total <= 1e-6:
        return intervals[0][0]
    offset = total * clamped
    for (start, end), duration, weight in zip(intervals, durations, effective_weights):
        if offset <= weight + 1e-6:
            interval_progress = 0.0 if weight <= 1e-6 else min(1.0, max(0.0, offset / weight))
            return min(end, start + duration * interval_progress)
        offset -= weight
    return intervals[-1][1]

def guard_source_cursor_no_early_jump(
    source_cursor: float,
    *,
    fragments: list[Fragment],
    available_shots: list[Shot],
    source_intervals: list[tuple[float, float]],
    min_visual_clip: float,
    max_source_drift_s: float,
) -> float:
    if len(source_intervals) < 2 or not fragments or not available_shots:
        return source_cursor
    last_fragment = fragments[-1]
    last_interval_index = source_interval_index(source_intervals, last_fragment.src_out)
    cursor_interval_index = source_interval_index(source_intervals, source_cursor)
    if (
        last_interval_index is None
        or cursor_interval_index is None
        or cursor_interval_index < last_interval_index
    ):
        return source_cursor
    if cursor_interval_index == last_interval_index:
        interval_start, interval_end = source_intervals[last_interval_index]
        lower_bound = max(interval_start, last_fragment.src_out)
        if (
            source_cursor > lower_bound + 1e-6
            and not interval_has_available_source_after(
                available_shots,
                (interval_start, interval_end),
                source_cursor,
                min_visual_clip=min_visual_clip,
            )
            and interval_has_available_source_after(
                available_shots,
                (interval_start, interval_end),
                lower_bound,
                min_visual_clip=min_visual_clip,
            )
        ):
            return lower_bound
        return source_cursor
    prior_interval_end = source_intervals[last_interval_index][1]
    if source_cursor - prior_interval_end > max_source_drift_s:
        return source_cursor
    for interval_index in range(last_interval_index, cursor_interval_index):
        interval_start, interval_end = source_intervals[interval_index]
        lower_bound = max(interval_start, last_fragment.src_out) if interval_index == last_interval_index else interval_start
        if interval_has_available_source_after(
            available_shots,
            (interval_start, interval_end),
            lower_bound,
            min_visual_clip=min_visual_clip,
        ):
            return lower_bound
    return source_cursor

def source_interval_index(
    intervals: list[tuple[float, float]],
    position: float,
    *,
    epsilon: float = 1e-3,
) -> int | None:
    for index, (start, end) in enumerate(intervals):
        if start - epsilon <= position <= end + epsilon:
            return index
    return None

def interval_has_available_source_after(
    shots: list[Shot],
    interval: tuple[float, float],
    lower_bound: float,
    *,
    min_visual_clip: float,
) -> bool:
    minimum = max(min_visual_clip, 1e-6)
    interval_start, interval_end = interval
    if interval_end - max(interval_start, lower_bound) + 1e-6 < minimum:
        return False
    for shot in shots:
        for start, end in shot_source_ranges(shot, [interval]):
            usable_start = max(start, lower_bound)
            if end - usable_start + 1e-6 >= minimum:
                return True
    return False


def choose_source_range(
    ranges: list[tuple[float, float]],
    source_cursor: float,
) -> tuple[float, float] | None:
    if not ranges:
        return None
    return min(
        ranges,
        key=lambda item: (
            0 if item[0] <= source_cursor < item[1] else 1 if item[0] >= source_cursor else 2,
            0.0 if item[0] <= source_cursor < item[1] else abs(item[0] - source_cursor),
            item[0],
        ),
    )


def ordered_fill_source_start(
    src_start: float,
    src_end: float,
    source_cursor: float,
    *,
    strict_ordered_fill: bool,
    min_visual_clip: float,
) -> float:
    if source_cursor <= src_start or source_cursor >= src_end:
        return src_start
    if not strict_ordered_fill:
        return source_cursor
    if source_cursor - src_start < max(min_visual_clip, 1e-6):
        return src_start
    return source_cursor

def ordered_fill_short_bridge_allowed(
    *,
    selected_range: tuple[float, float],
    source_cursor: float,
    source_intervals: list[tuple[float, float]],
    available_shots: list[Shot],
    min_visual_clip: float,
    strict_ordered_fill: bool,
    match_strategy: str,
) -> bool:
    if not strict_ordered_fill or match_strategy != "chronological" or min_visual_clip <= 0:
        return False
    interval_index = source_interval_index(source_intervals, source_cursor)
    if interval_index is None:
        return False
    interval_start, interval_end = source_intervals[interval_index]
    if selected_range[1] < interval_end - 1e-6:
        return False
    return not interval_has_available_source_after(
        available_shots,
        (interval_start, interval_end),
        source_cursor,
        min_visual_clip=min_visual_clip,
    )

def uncovered_source_ranges(
    shot: Shot,
    window_start: float,
    window_end: float,
    used_ranges: list[tuple[float, float]],
    *,
    source_intervals: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    intervals = source_intervals or [(window_start, window_end)]
    for start, end in shot_source_ranges(shot, intervals):
        clipped = sorted(
            (max(start, used_start), min(end, used_end))
            for used_start, used_end in used_ranges
            if used_end > start and used_start < end
        )
        cursor = start
        for used_start, used_end in clipped:
            if used_start > cursor + 1e-6:
                output.append((cursor, used_start))
            cursor = max(cursor, used_end)
        if cursor < end - 1e-6:
            output.append((cursor, end))
    return output


def choose_uncovered_range(
    ranges: list[tuple[float, float]],
    source_cursor: float,
) -> tuple[float, float] | None:
    if not ranges:
        return None
    return min(
        ranges,
        key=lambda item: (
            0 if item[0] <= source_cursor < item[1] else 1 if item[0] >= source_cursor else 2,
            abs(item[0] - source_cursor),
        ),
    )


def source_overlap_duration(
    start: float,
    end: float,
    used_ranges: list[tuple[float, float]],
) -> float:
    return sum(max(0.0, min(end, used_end) - max(start, used_start)) for used_start, used_end in used_ranges)


def least_overlap_range(
    shot: Shot,
    window_start: float,
    window_end: float,
    clip_len: float,
    used_ranges: list[tuple[float, float]],
    source_cursor: float,
    *,
    source_intervals: list[tuple[float, float]] | None = None,
) -> tuple[float, float] | None:
    intervals = source_intervals or [(window_start, window_end)]
    candidates: list[tuple[float, float]] = []
    for start, end in shot_source_ranges(shot, intervals):
        range_clip_len = min(clip_len, end - start)
        if range_clip_len <= 1e-6:
            continue
        latest_start = max(start, end - range_clip_len)
        candidate_starts = {start, latest_start}
        for used_start, used_end in used_ranges:
            candidate_starts.add(min(latest_start, max(start, used_end)))
            candidate_starts.add(min(latest_start, max(start, used_start - range_clip_len)))
        for candidate in candidate_starts:
            candidates.append((candidate, min(end, candidate + range_clip_len)))
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            source_overlap_duration(item[0], item[1], used_ranges),
            abs(item[0] - source_cursor),
            item[0],
        ),
    )


def avoid_adjacent_repeat_in_tier(
    ranked: list[Shot],
    *,
    previous_shot_index: int | None,
    source_cursor: float,
    max_source_drift_s: float,
) -> list[Shot]:
    if previous_shot_index is None or len(ranked) < 2 or ranked[0].index != previous_shot_index:
        return ranked
    previous_tier = chronology_tier(
        ranked[0],
        source_cursor,
        max_source_drift_s=max_source_drift_s,
    )[0]
    alternatives = [
        shot
        for shot in ranked[1:]
        if chronology_tier(shot, source_cursor, max_source_drift_s=max_source_drift_s)[0] == previous_tier
    ]
    return alternatives if alternatives else ranked


def rank_for_ordered_fill(
    ranked: list[Shot],
    reuse_counts: dict[int, int],
    weights: ScoringWeights,
    semantic_scores: dict[tuple[int, int], float],
    visual_scores: dict[tuple[int, int], float],
    beat_id: int,
    source_cursor: float,
    match_strategy: str = "hybrid",
    chronology_weight: float = 0.70,
    max_source_drift_s: float = 12.0,
    strict_ordered_fill: bool = False,
    visual_priority: bool = False,
) -> list[Shot]:
    def sort_key(shot: Shot) -> tuple[float, ...]:
        tier, distance = chronology_tier(shot, source_cursor, max_source_drift_s=max_source_drift_s)
        score = score_shot(
            shot,
            reuse_counts.get(shot.index, 0),
            weights,
            semantic_scores.get((beat_id, shot.index), 0.0),
            visual_scores.get((beat_id, shot.index), 0.0),
        )
        visual_score = visual_scores.get((beat_id, shot.index), 0.0)
        if match_strategy == "chronological":
            if strict_ordered_fill:
                if visual_priority:
                    return (1.0 if tier >= 2 else 0.0, distance, -visual_score, -score, shot.index)
                return (1.0 if tier >= 2 else 0.0, distance, -score, shot.index)
            if visual_priority:
                return (float(tier), -visual_score, -score, distance, shot.index)
            return (float(tier), -score, distance, shot.index)
        if match_strategy == "hybrid":
            drift_penalty = chronology_weight * min(1.0, distance / max(max_source_drift_s, 0.001))
            if visual_priority:
                return (1 if tier in {1, 3} else 0, -visual_score, -(score - drift_penalty), distance, shot.index)
            return (1 if tier in {1, 3} else 0, -(score - drift_penalty), distance, shot.index)
        return (0.0, -score, distance, shot.index)

    return sorted(ranked, key=sort_key)


def chronology_tier(shot: Shot, source_cursor: float, *, max_source_drift_s: float) -> tuple[int, float]:
    if shot.tc_start <= source_cursor <= shot.tc_end:
        distance = 0.0
    else:
        distance = min(abs(shot.tc_start - source_cursor), abs(shot.tc_end - source_cursor))
    is_before_cursor = shot.tc_end < source_cursor
    beyond_drift = distance > max_source_drift_s
    if beyond_drift:
        return (3 if is_before_cursor else 2), distance
    return (1 if is_before_cursor else 0), distance

def choose_diverse_shot(
    ranked: list[Shot],
    reuse_counts: dict[int, int],
    weights: ScoringWeights,
    semantic_scores: dict[tuple[int, int], float],
    visual_scores: dict[tuple[int, int], float],
    beat_id: int,
    previous_shot_index: int | None,
    min_alternative_ratio: float,
    adjacent_penalty: float,
    source_cursor: float | None = None,
    max_source_drift_s: float = 12.0,
    enforce_chronology_tier: bool = False,
) -> Shot:
    if not ranked:
        raise ValueError("cannot choose from empty candidates")
    top = ranked[0]
    top_score = score_shot(
        top,
        reuse_counts.get(top.index, 0),
        weights,
        semantic_scores.get((beat_id, top.index), 0.0),
        visual_scores.get((beat_id, top.index), 0.0),
    )
    if previous_shot_index is None or top.index != previous_shot_index:
        return top
    threshold = max(top_score * min_alternative_ratio, top_score - adjacent_penalty)
    top_tier = chronology_tier(top, source_cursor, max_source_drift_s=max_source_drift_s)[0] if source_cursor is not None else None
    for candidate in ranked[1:]:
        if enforce_chronology_tier and source_cursor is not None:
            candidate_tier = chronology_tier(candidate, source_cursor, max_source_drift_s=max_source_drift_s)[0]
            if candidate_tier != top_tier:
                continue
        candidate_score = score_shot(
            candidate,
            reuse_counts.get(candidate.index, 0),
            weights,
            semantic_scores.get((beat_id, candidate.index), 0.0),
            visual_scores.get((beat_id, candidate.index), 0.0),
        )
        if candidate.index != previous_shot_index and candidate_score >= threshold:
            return candidate
    return top

def trim_fragments_to_duration(fragments: list[Fragment], target_duration: float, *, min_visual_clip: float = 0.0) -> list[Fragment]:
    output: list[Fragment] = []
    remaining = target_duration
    for fragment in fragments:
        if remaining <= 1e-6:
            break
        duration = min(fragment.duration, remaining)
        if duration < 0.05 and output:
            break
        output.append(
            Fragment(
                src=fragment.src,
                src_in=fragment.src_in,
                src_out=round(fragment.src_in + duration, 3),
                beat_id=fragment.beat_id,
                shot_index=fragment.shot_index,
                reused=fragment.reused,
                speed=fragment.speed,
            )
        )
        remaining = round(remaining - duration, 6)
    return coalesce_short_fragments(output, target_duration=target_duration, min_visual_clip=min_visual_clip)

def coalesce_short_fragments(fragments: list[Fragment], *, target_duration: float, min_visual_clip: float) -> list[Fragment]:
    if min_visual_clip <= 0 or len(fragments) <= 1:
        return fragments
    output = list(fragments)
    index = 0
    while index < len(output):
        fragment = output[index]
        if fragment.duration >= min_visual_clip or len(output) == 1:
            index += 1
            continue
        if index > 0 and output[index - 1].shot_index == fragment.shot_index and abs(output[index - 1].src_out - fragment.src_in) <= 1e-3:
            previous = output[index - 1]
            output[index - 1] = replace(previous, src_out=fragment.src_out)
            del output[index]
            index = max(0, index - 1)
            continue
        if index + 1 < len(output) and output[index + 1].shot_index == fragment.shot_index and abs(fragment.src_out - output[index + 1].src_in) <= 1e-3:
            next_fragment = output[index + 1]
            output[index + 1] = replace(next_fragment, src_in=fragment.src_in)
            del output[index]
            continue
        index += 1
    total = sum(fragment.duration for fragment in output)
    diff = round(target_duration - total, 6)
    if output and abs(diff) > 1e-6 and output[-1].duration + diff > 0 and abs(diff) <= 0.002:
        output[-1] = replace(output[-1], src_out=round(output[-1].src_out + diff, 3))
    return output


def assign_timeline(fragments: list[Fragment], timing: BeatTiming) -> list[EdlPlacement]:
    placements: list[EdlPlacement] = []
    cursor = timing.tl_start
    for fragment in fragments:
        duration = fragment.duration / max(fragment.speed, 1e-6)
        tl_end = min(timing.tl_end, round(cursor + duration, 3))
        source_duration = max(0.001, (tl_end - cursor) * fragment.speed)
        placements.append(
            EdlPlacement(
                tl_start=round(cursor, 3),
                tl_end=round(tl_end, 3),
                src=fragment.src,
                src_in=fragment.src_in,
                src_out=round(min(fragment.src_out, fragment.src_in + source_duration), 3),
                beat_id=fragment.beat_id,
                shot_index=fragment.shot_index,
                reused=fragment.reused,
                speed=fragment.speed,
            )
        )
        cursor = tl_end
        if cursor >= timing.tl_end - 1e-6:
            break
    return placements


def fill_timeline_gaps(
    placements: list[EdlPlacement],
    total_duration: float,
    *,
    min_visual_clip: float = 0.0,
    shots: list[Shot] | None = None,
    min_pause_speed: float = 0.90,
) -> list[EdlPlacement]:
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id))
    if not ordered:
        return ordered
    output: list[EdlPlacement] = []
    shots_by_index = {shot.index: shot for shot in shots or []}
    previous: EdlPlacement | None = None
    for placement in ordered:
        if previous is not None and placement.tl_start > previous.tl_end + 1e-3:
            gap = round(placement.tl_start - previous.tl_end, 3)
            if min_visual_clip > 0 and gap < min_visual_clip and output:
                updated_previous, updated_next, absorbed = absorb_short_gap(
                    output[-1],
                    placement,
                    gap,
                    shots_by_index=shots_by_index,
                    min_pause_speed=min_pause_speed,
                )
                if absorbed:
                    output[-1] = updated_previous
                    placement = updated_next
                    previous = output[-1]
                else:
                    output.append(make_pause_filler(previous, previous.tl_end, placement.tl_start, gap, shots_by_index=shots_by_index))
            else:
                output.append(make_pause_filler(previous, previous.tl_end, placement.tl_start, gap, shots_by_index=shots_by_index))
        output.append(placement)
        previous = placement
    if previous is not None and total_duration > previous.tl_end + 1e-3:
        gap = round(total_duration - previous.tl_end, 3)
        if min_visual_clip > 0 and gap < min_visual_clip and output:
            stretched = stretch_placement(output[-1], total_duration, min_speed=min_pause_speed)
            if stretched is not None:
                output[-1] = stretched
            else:
                output.append(make_pause_filler(previous, previous.tl_end, total_duration, gap, shots_by_index=shots_by_index))
        else:
            output.append(make_pause_filler(previous, previous.tl_end, total_duration, gap, shots_by_index=shots_by_index))
    return output

def split_long_placements(placements: list[EdlPlacement], *, max_clip: float) -> list[EdlPlacement]:
    if max_clip <= 0:
        return placements
    output: list[EdlPlacement] = []
    for placement in sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id)):
        total_duration = placement.tl_end - placement.tl_start
        if total_duration <= max_clip + 1e-6:
            output.append(placement)
            continue
        n_chunks = max(2, ceil(total_duration / max_clip))
        chunk_duration = total_duration / n_chunks
        tl_cursor = placement.tl_start
        src_cursor = placement.src_in
        for index in range(n_chunks):
            tl_start = round(tl_cursor, 3)
            src_in = round(src_cursor, 3)
            if index == n_chunks - 1:
                tl_end = placement.tl_end
                src_out = placement.src_out
            else:
                tl_end = round(tl_cursor + chunk_duration, 3)
                actual_duration = tl_end - tl_start
                src_out = round(src_cursor + actual_duration * placement.speed, 3)
            output.append(placement.model_copy(update={
                "tl_start": tl_start,
                "tl_end": round(tl_end, 3),
                "src_in": src_in,
                "src_out": round(src_out, 3),
            }))
            tl_cursor = tl_end
            src_cursor = src_out
    return output

def extend_placement(placement: EdlPlacement, tl_end: float) -> EdlPlacement:
    extension = max(0.0, tl_end - placement.tl_end)
    return placement.model_copy(update={
        "tl_end": round(tl_end, 3),
        "src_out": round(placement.src_out + extension * placement.speed, 3),
    })


def stretch_placement(placement: EdlPlacement, tl_end: float, *, min_speed: float) -> EdlPlacement | None:
    target_duration = tl_end - placement.tl_start
    source_duration = placement.src_out - placement.src_in
    if target_duration <= 0 or source_duration <= 0:
        return None
    speed = source_duration / target_duration
    if speed < min_speed:
        return None
    return placement.model_copy(update={"tl_end": round(tl_end, 3), "speed": round(speed, 6)})


def absorb_short_gap(
    previous: EdlPlacement,
    following: EdlPlacement,
    gap: float,
    *,
    shots_by_index: dict[int, Shot],
    min_pause_speed: float,
) -> tuple[EdlPlacement, EdlPlacement, bool]:
    previous_shot = shots_by_index.get(previous.shot_index)
    previous_source_extension = gap * previous.speed
    if previous_shot is not None and previous.src_out + previous_source_extension <= previous_shot.tc_end + 1e-6:
        return extend_placement(previous, following.tl_start), following, True
    following_shot = shots_by_index.get(following.shot_index)
    following_source_extension = gap * following.speed
    if following_shot is not None and following.src_in - following_source_extension >= following_shot.tc_start - 1e-6:
        updated = following.model_copy(
            update={
                "tl_start": previous.tl_end,
                "src_in": round(following.src_in - following_source_extension, 3),
            }
        )
        return previous, updated, True
    stretched = stretch_placement(previous, following.tl_start, min_speed=min_pause_speed)
    if stretched is not None:
        return stretched, following, True
    previous_duration = previous.tl_end - previous.tl_start
    following_duration = following.tl_end - following.tl_start
    previous_source_duration = previous.src_out - previous.src_in
    following_source_duration = following.src_out - following.src_in
    previous_capacity = max(0.0, previous_source_duration / min_pause_speed - previous_duration)
    following_capacity = max(0.0, following_source_duration / min_pause_speed - following_duration)
    if previous_capacity + following_capacity >= gap - 1e-6:
        previous_extension = min(gap, previous_capacity)
        split_tl = floor((previous.tl_end + previous_extension) * 1000 + 1e-9) / 1000
        stretched_previous = stretch_placement(previous, split_tl, min_speed=min_pause_speed)
        following_target_duration = following.tl_end - split_tl
        following_speed = following_source_duration / following_target_duration if following_target_duration > 0 else 0.0
        if stretched_previous is not None and following_speed >= min_pause_speed - 1e-6:
            stretched_following = following.model_copy(
                update={"tl_start": split_tl, "speed": round(following_speed, 6)}
            )
            return stretched_previous, stretched_following, True
    return previous, following, False

def make_pause_filler(
    previous: EdlPlacement,
    tl_start: float,
    tl_end: float,
    duration: float,
    *,
    shots_by_index: dict[int, Shot] | None = None,
) -> EdlPlacement:
    shot = (shots_by_index or {}).get(previous.shot_index)
    if shot is not None:
        source_duration = min(max(duration, 0.001), shot.duration)
        src_out = min(max(previous.src_out, shot.tc_start + source_duration), shot.tc_end)
        src_in = max(shot.tc_start, src_out - source_duration)
        if src_out - src_in < source_duration - 1e-6:
            src_in = shot.tc_start
            src_out = min(shot.tc_end, src_in + source_duration)
    else:
        source_duration = max(duration, 0.001)
        src_out = previous.src_out
        src_in = max(previous.src_in, src_out - source_duration)
    speed = max(0.001, (src_out - src_in) / max(duration, 0.001))
    return EdlPlacement(
        tl_start=round(tl_start, 3),
        tl_end=round(tl_end, 3),
        src=previous.src,
        src_in=round(src_in, 3),
        src_out=round(src_out, 3),
        beat_id=previous.beat_id,
        shot_index=previous.shot_index,
        reused=True,
        speed=round(speed, 6),
    )
