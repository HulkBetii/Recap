from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from common.inputs import load_shots
from common.schema import (
    BeatTiming,
    EdlMeta,
    EdlPlacement,
    EdlSourceMap,
    SeriesEvent,
    SeriesEventBank,
    SeriesReviewBeat,
    SeriesSourceRef,
    Shot,
    validate_beats_timing,
    validate_edl,
    validate_series_review_script,
    write_json,
)

ALGORITHM_VERSION = "series-v2"
DYNAMIC_ALGORITHM_VERSION = "series-v3-dynamic-anime"
DYNAMIC_EDGE_INSET_S = 0.75

DYNAMIC_CLIP_RANGES: dict[str, tuple[float, float]] = {
    "fast": (1.5, 2.4),
    "normal": (1.8, 3.0),
    "dramatic": (2.5, 4.0),
}
FAST_EVENT_TYPES = {"hook", "conflict", "action"}
DRAMATIC_EVENT_TYPES = {"reveal", "climax", "ending"}


class SeriesMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClipCandidate:
    episode_key: str
    src_key: str
    source_path: str
    shot: Shot
    start: float
    end: float


@dataclass(frozen=True)
class SelectedClip:
    candidate: ClipCandidate
    start: float
    duration: float
    reused: bool = False
    fallback: bool = False


@dataclass(frozen=True)
class DynamicClipRule:
    event_id: str
    event_type: str
    importance: float
    min_clip: float
    max_clip: float
    target_clip: float


@dataclass(frozen=True)
class ClipBoundary:
    src_key: str
    shot_index: int
    start: float
    end: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match multi-episode series review beats to footage.")
    parser.add_argument("--series-review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--episode-run-dir", action="append", default=[], help="Episode artifact dir as episode_key=path")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-source-map", required=True, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--event-bank", default=None, type=Path)
    parser.add_argument("--clip-profile", default="legacy", choices=["legacy", "dynamic_anime"])
    parser.add_argument("--min-clip", default=3.0, type=float)
    parser.add_argument("--max-clip", default=5.0, type=float)
    parser.add_argument("--min-visual-clip", default=0.6, type=float)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--work-dir", default=Path("work") / "series_match", type=Path)
    return parser


def parse_episode_run_dirs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise SeriesMatchError("--episode-run-dir must use episode_key=path")
        key, raw_path = value.split("=", 1)
        key = key.strip()
        if not key:
            raise SeriesMatchError("--episode-run-dir episode_key cannot be empty")
        if key in result:
            raise SeriesMatchError(f"duplicate episode run dir for {key}")
        result[key] = Path(raw_path).expanduser().resolve()
    if not result:
        raise SeriesMatchError("at least one --episode-run-dir is required")
    return result


def load_series_beats(path: Path) -> list[SeriesReviewBeat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    beats = [SeriesReviewBeat.model_validate(item) for item in data]
    return validate_series_review_script(beats)


def load_timings(path: Path) -> list[BeatTiming]:
    data = json.loads(path.read_text(encoding="utf-8"))
    timings = [BeatTiming.model_validate(item) for item in data]
    if len(timings) > 1:
        pause_s = timings[1].tl_start - timings[0].tl_end
        pause_s = max(0.0, round(pause_s, 3))
    else:
        pause_s = 0.0
    return validate_beats_timing(timings, pause_s=pause_s)


def load_event_bank(path: Path) -> SeriesEventBank:
    return SeriesEventBank.model_validate_json(path.read_text(encoding="utf-8"))


def source_map_from_beats(beats: list[SeriesReviewBeat]) -> EdlSourceMap:
    sources: dict[str, str] = {}
    for beat in beats:
        for ref in beat.source_refs:
            sources[ref.src] = str(Path(ref.source_path).expanduser().resolve())
    return EdlSourceMap(version=1, sources=sources, created_at=datetime.now(timezone.utc))


def candidate_score(shot: Shot) -> float:
    return float(shot.motion_score or 0.0) * 0.65 + float(shot.brightness or 0.0) * 0.12


def _inset_shot_edges(
    *,
    start: float,
    end: float,
    shot: Shot,
    edge_inset_s: float,
    min_clip: float,
) -> tuple[float, float]:
    if edge_inset_s <= 0:
        return start, end
    touches_start = abs(start - shot.tc_start) <= 1e-6
    touches_end = abs(end - shot.tc_end) <= 1e-6
    if not (touches_start and touches_end):
        return start, end
    if end - start - 2 * edge_inset_s < min_clip - 1e-6:
        return start, end
    return start + edge_inset_s, end - edge_inset_s


def ref_candidates(
    ref: SeriesSourceRef,
    shots: list[Shot],
    *,
    edge_inset_s: float = 0.0,
    inset_min_clip: float = 0.6,
) -> list[ClipCandidate]:
    candidates: list[ClipCandidate] = []
    source_path = str(Path(ref.source_path).expanduser().resolve())
    for shot in shots:
        if shot.is_story is False or shot.is_usable is False or shot.is_end_credit is True:
            continue
        start = max(shot.tc_start, ref.src_tc_start)
        end = min(shot.tc_end, ref.src_tc_end)
        if end <= start:
            continue
        start, end = _inset_shot_edges(
            start=start,
            end=end,
            shot=shot,
            edge_inset_s=edge_inset_s,
            min_clip=inset_min_clip,
        )
        candidates.append(
            ClipCandidate(
                episode_key=ref.episode_key,
                src_key=ref.src,
                source_path=source_path,
                shot=shot,
                start=start,
                end=end,
            )
        )
    return sorted(candidates, key=lambda item: (item.start, -candidate_score(item.shot), item.shot.index))


def fallback_candidates(
    ref: SeriesSourceRef,
    shots: list[Shot],
    *,
    edge_inset_s: float = 0.0,
    inset_min_clip: float = 0.6,
) -> list[ClipCandidate]:
    source_path = str(Path(ref.source_path).expanduser().resolve())
    values: list[ClipCandidate] = []
    for shot in shots:
        if shot.is_story is False or shot.is_usable is False or shot.is_end_credit is True:
            continue
        start, end = _inset_shot_edges(
            start=shot.tc_start,
            end=shot.tc_end,
            shot=shot,
            edge_inset_s=edge_inset_s,
            min_clip=inset_min_clip,
        )
        values.append(
            ClipCandidate(
                episode_key=ref.episode_key,
                src_key=ref.src,
                source_path=source_path,
                shot=shot,
                start=start,
                end=end,
            )
        )
    return sorted(values, key=lambda item: (abs(item.start - ref.src_tc_start), item.start, -candidate_score(item.shot)))


def timing_windows(timings: list[BeatTiming]) -> dict[int, tuple[float, float]]:
    windows: dict[int, tuple[float, float]] = {}
    for index, timing in enumerate(timings):
        end = timings[index + 1].tl_start if index + 1 < len(timings) else timing.tl_end
        windows[timing.beat_id] = (timing.tl_start, end)
    return windows


def add_clip(
    placements: list[EdlPlacement],
    *,
    beat_id: int,
    tl_cursor: float,
    duration: float,
    candidate: ClipCandidate,
    src_start: float | None = None,
    reused: bool = False,
) -> None:
    source_start = candidate.start if src_start is None else src_start
    placements.append(
        EdlPlacement(
            tl_start=round(tl_cursor, 3),
            tl_end=round(tl_cursor + duration, 3),
            src=candidate.src_key,
            src_in=round(source_start, 3),
            src_out=round(source_start + duration, 3),
            beat_id=beat_id,
            shot_index=candidate.shot.index,
            reused=reused,
            speed=1.0,
        )
    )


def choose_clip_duration(
    *,
    available: float,
    remaining: float,
    max_clip: float,
    min_visual_clip: float,
    min_clip: float | None = None,
    target_clip: float | None = None,
) -> float:
    duration = min(max_clip, available, remaining)
    if min_clip is not None and target_clip is not None and remaining >= min_clip - 1e-6:
        minimum_count = max(1, math.ceil((remaining - 1e-6) / max_clip))
        maximum_count = max(1, math.floor((remaining + 1e-6) / min_clip))
        preferred_count = round(remaining / target_clip)
        preferred_count = max(minimum_count, min(maximum_count, preferred_count))
        balanced_duration = remaining / preferred_count
        if available + 1e-6 >= balanced_duration:
            duration = min(balanced_duration, available)
    elif min_clip is not None:
        preferred_count = max(1, math.ceil((remaining - 1e-6) / max_clip))
        if remaining + 1e-6 >= preferred_count * min_clip:
            balanced_duration = remaining / preferred_count
            if available + 1e-6 >= balanced_duration:
                duration = min(balanced_duration, available)
    tail_after = remaining - duration
    if (
        min_clip is not None
        and remaining >= 2 * min_clip - 1e-6
        and 1e-6 < tail_after < min_clip
        and duration - (min_clip - tail_after) >= min_clip
    ):
        duration -= min_clip - tail_after
        tail_after = remaining - duration
    if 1e-6 < tail_after < min_visual_clip:
        shrink_by = min_visual_clip - tail_after
        if duration - shrink_by >= min_visual_clip:
            duration -= shrink_by
        else:
            # Leave this candidate for a later shot that can absorb the
            # remainder without creating a sub-minimum flash fragment.
            return 0.0
    return duration


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _available_parts(
    candidate: ClipCandidate,
    used_intervals: dict[str, list[tuple[float, float]]],
    *,
    allow_reuse: bool,
) -> list[tuple[float, float]]:
    if allow_reuse:
        return [(candidate.start, candidate.end)]
    cursor = candidate.start
    parts: list[tuple[float, float]] = []
    for used_start, used_end in _merge_intervals(used_intervals.get(candidate.src_key, [])):
        if used_end <= cursor + 1e-6:
            continue
        if used_start >= candidate.end - 1e-6:
            break
        if used_start > cursor + 1e-6:
            parts.append((cursor, min(used_start, candidate.end)))
        cursor = max(cursor, used_end)
        if cursor >= candidate.end - 1e-6:
            break
    if cursor < candidate.end - 1e-6:
        parts.append((cursor, candidate.end))
    return parts


def _dedupe_candidates(candidates: list[ClipCandidate]) -> list[ClipCandidate]:
    unique: dict[tuple[str, int, float, float], ClipCandidate] = {}
    for candidate in candidates:
        key = (candidate.src_key, candidate.shot.index, candidate.start, candidate.end)
        unique.setdefault(key, candidate)
    return sorted(unique.values(), key=lambda item: (item.start, item.end, item.shot.index))


def _candidate_capacity(candidates: list[ClipCandidate], min_visual_clip: float) -> float:
    # Keep shot boundaries distinct: two adjacent sub-floor shots cannot be
    # joined into one placement without violating the visual hard floor.
    return sum(
        candidate.end - candidate.start
        for candidate in _dedupe_candidates(candidates)
        if candidate.end - candidate.start >= min_visual_clip - 1e-6
    )


def _dynamic_rule(beat: SeriesReviewBeat, event: SeriesEvent, *, max_clip: float) -> DynamicClipRule:
    event_type = event.event_type.strip().lower().replace("-", "_").replace(" ", "_")
    if beat.is_hook or event_type in FAST_EVENT_TYPES:
        range_key = "fast"
        resolved_type = "hook" if beat.is_hook else event_type
    elif event_type in DRAMATIC_EVENT_TYPES:
        range_key = "dramatic"
        resolved_type = event_type
    else:
        range_key = "normal"
        resolved_type = event_type or "neutral"
    range_min, range_max = DYNAMIC_CLIP_RANGES[range_key]
    effective_max = min(range_max, max_clip)
    if effective_max + 1e-6 < range_min:
        raise SeriesMatchError(
            f"dynamic_anime event {event.event_id} requires --max-clip >= {range_min:.1f}s"
        )
    target = range_min + (effective_max - range_min) * event.importance
    return DynamicClipRule(
        event_id=event.event_id,
        event_type=resolved_type,
        importance=event.importance,
        min_clip=range_min,
        max_clip=effective_max,
        target_clip=target,
    )


def _selected_boundary(item: SelectedClip) -> ClipBoundary:
    return ClipBoundary(
        src_key=item.candidate.src_key,
        shot_index=item.candidate.shot.index,
        start=item.start,
        end=item.start + item.duration,
    )


def _placement_boundary(item: EdlPlacement) -> ClipBoundary:
    return ClipBoundary(
        src_key=item.src,
        shot_index=int(item.shot_index if item.shot_index is not None else -1),
        start=item.src_in,
        end=item.src_out,
    )


def _adjacency_conflicts(
    previous: ClipBoundary | None,
    candidate: ClipCandidate,
    start: float,
    end: float,
) -> list[str]:
    if previous is None or previous.src_key != candidate.src_key:
        return []
    conflicts: list[str] = []
    if previous.shot_index == candidate.shot.index:
        conflicts.append("same_shot")
    if abs(previous.end - start) <= 1e-6 or abs(end - previous.start) <= 1e-6:
        conflicts.append("contiguous_source")
    return conflicts


def _allocate_ref_quotas(
    *,
    beat_id: int,
    event_ids: list[str],
    duration: float,
    capacities: list[float],
    min_clip: float,
    min_visual_clip: float,
    required_floors: list[float] | None = None,
) -> list[float]:
    count = len(event_ids)
    if duration + 1e-6 < count * min_visual_clip:
        raise SeriesMatchError(
            f"beat {beat_id} duration {duration:.3f}s cannot represent {count} source refs "
            f"at --min-visual-clip {min_visual_clip:.3f}s"
        )
    if required_floors is None:
        floors = [min_clip if duration + 1e-6 >= count * min_clip else min_visual_clip] * count
    else:
        if len(required_floors) != count:
            raise SeriesMatchError("dynamic clip floor count must match source refs")
        floors = required_floors if duration + 1e-6 >= sum(required_floors) else [min_visual_clip] * count
    for event_id, capacity, floor in zip(event_ids, capacities, floors, strict=True):
        if capacity + 1e-6 < floor:
            raise SeriesMatchError(
                f"beat {beat_id} event {event_id} has only {capacity:.3f}s usable story footage; "
                f"requires at least {floor:.3f}s"
            )

    if required_floors is None:
        quotas = [min(duration / count, capacity) for capacity in capacities]
    else:
        quotas = list(floors)
    remaining = duration - sum(quotas)
    while remaining > 1e-6:
        eligible = [index for index, capacity in enumerate(capacities) if capacity - quotas[index] > 1e-6]
        if not eligible:
            raise SeriesMatchError(
                f"beat {beat_id} cannot fill source refs {', '.join(event_ids)} with usable story footage"
            )
        share = remaining / len(eligible)
        distributed = 0.0
        for index in eligible:
            addition = min(share, capacities[index] - quotas[index])
            quotas[index] += addition
            distributed += addition
        if distributed <= 1e-6:
            raise SeriesMatchError(
                f"beat {beat_id} cannot allocate footage across source refs {', '.join(event_ids)}"
            )
        remaining -= distributed
    return quotas


def _select_clips(
    *,
    candidates: list[ClipCandidate],
    target: float,
    used_intervals: dict[str, list[tuple[float, float]]],
    min_clip: float,
    max_clip: float,
    min_visual_clip: float,
    fallback: bool,
    allow_reuse: bool = False,
    allow_short_clips: bool = True,
    round_robin: bool = False,
    target_clip: float | None = None,
    previous_clip: ClipBoundary | None = None,
    adjacency_diagnostics: list[dict[str, object]] | None = None,
    event_id: str | None = None,
) -> tuple[list[SelectedClip], float]:
    candidates = _dedupe_candidates(candidates)
    parts: list[tuple[ClipCandidate, float, float]] = []
    for candidate in candidates:
        for start, end in _available_parts(candidate, used_intervals, allow_reuse=allow_reuse):
            if end - start >= min_visual_clip - 1e-6:
                parts.append((candidate, start, end))

    normal_capacity = sum(end - start for _, start, end in parts if end - start >= min_clip - 1e-6)
    if not allow_short_clips or normal_capacity + 1e-6 >= target:
        parts = [part for part in parts if part[2] - part[1] >= min_clip - 1e-6]

    selected: list[SelectedClip] = []
    remaining = target
    selection_floor = min_visual_clip if allow_short_clips else min_clip
    if round_robin:
        queued_parts = list(parts)
        previous = previous_clip
        while remaining > 1e-6:
            viable: list[tuple[int, float, list[str]]] = []
            for index, (candidate, part_start, part_end) in enumerate(queued_parts):
                if part_end - part_start < selection_floor - 1e-6:
                    continue
                duration = choose_clip_duration(
                    available=part_end - part_start,
                    remaining=remaining,
                    max_clip=max_clip,
                    min_visual_clip=min_visual_clip,
                    min_clip=min_clip,
                    target_clip=target_clip,
                )
                if duration + 1e-6 < selection_floor:
                    continue
                viable.append(
                    (
                        index,
                        duration,
                        _adjacency_conflicts(previous, candidate, part_start, part_start + duration),
                    )
                )
            if not viable:
                break
            chosen = next((item for item in viable if not item[2]), viable[0])
            part_index, duration, conflicts = chosen
            candidate, part_start, part_end = queued_parts.pop(part_index)
            item = SelectedClip(
                candidate=candidate,
                start=part_start,
                duration=duration,
                reused=allow_reuse,
                fallback=fallback,
            )
            selected.append(item)
            if conflicts and adjacency_diagnostics is not None:
                adjacency_diagnostics.append(
                    {
                        "event_id": event_id,
                        "episode_key": candidate.episode_key,
                        "shot_index": candidate.shot.index,
                        "src_in": round(part_start, 3),
                        "duration_s": round(duration, 3),
                        "conflicts": conflicts,
                        "reason": "no_distinct_candidate_capacity",
                        "episode_fallback": fallback,
                        "reused": allow_reuse,
                    }
                )
            used_intervals.setdefault(candidate.src_key, []).append((part_start, part_start + duration))
            next_start = part_start + duration
            if part_end - next_start >= selection_floor - 1e-6:
                queued_parts.append((candidate, next_start, part_end))
            previous = _selected_boundary(item)
            remaining -= duration
        return selected, max(0.0, remaining)

    for candidate, part_start, part_end in parts:
        cursor = part_start
        while remaining > 1e-6 and part_end - cursor >= selection_floor - 1e-6:
            duration = choose_clip_duration(
                available=part_end - cursor,
                remaining=remaining,
                max_clip=max_clip,
                min_visual_clip=min_visual_clip,
                min_clip=min_clip,
            )
            if duration + 1e-6 < selection_floor:
                break
            selected.append(
                SelectedClip(
                    candidate=candidate,
                    start=cursor,
                    duration=duration,
                    reused=allow_reuse,
                    fallback=fallback,
                )
            )
            used_intervals.setdefault(candidate.src_key, []).append((cursor, cursor + duration))
            cursor += duration
            remaining -= duration
        if remaining <= 1e-6:
            break
    return selected, max(0.0, remaining)


def _ref_plan(
    *,
    beat_id: int,
    ref: SeriesSourceRef,
    shots: list[Shot],
    quota: float,
    used_intervals: dict[str, list[tuple[float, float]]],
    min_clip: float,
    max_clip: float,
    min_visual_clip: float,
    round_robin: bool = False,
    target_clip: float | None = None,
    previous_clip: ClipBoundary | None = None,
    adjacency_diagnostics: list[dict[str, object]] | None = None,
    edge_inset_s: float = 0.0,
) -> list[SelectedClip]:
    strict = ref_candidates(ref, shots, edge_inset_s=edge_inset_s, inset_min_clip=min_clip)
    fallback = fallback_candidates(ref, shots, edge_inset_s=edge_inset_s, inset_min_clip=min_clip)
    selected, remaining = _select_clips(
        candidates=strict,
        target=quota,
        used_intervals=used_intervals,
        min_clip=min_clip,
        max_clip=max_clip,
        min_visual_clip=min_visual_clip,
        fallback=False,
        allow_short_clips=False,
        round_robin=round_robin,
        target_clip=target_clip,
        previous_clip=previous_clip,
        adjacency_diagnostics=adjacency_diagnostics,
        event_id=ref.event_id,
    )
    previous = _selected_boundary(selected[-1]) if selected else previous_clip
    if remaining > 1e-6:
        extra, remaining = _select_clips(
            candidates=fallback,
            target=remaining,
            used_intervals=used_intervals,
            min_clip=min_clip,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
            fallback=True,
            allow_short_clips=False,
            round_robin=round_robin,
            target_clip=target_clip,
            previous_clip=previous,
            adjacency_diagnostics=adjacency_diagnostics,
            event_id=ref.event_id,
        )
        selected.extend(extra)
        previous = _selected_boundary(extra[-1]) if extra else previous
    if remaining > 1e-6:
        extra, remaining = _select_clips(
            candidates=strict,
            target=remaining,
            used_intervals=used_intervals,
            min_clip=min_clip,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
            fallback=False,
            round_robin=round_robin,
            target_clip=target_clip,
            previous_clip=previous,
            adjacency_diagnostics=adjacency_diagnostics,
            event_id=ref.event_id,
        )
        selected.extend(extra)
        previous = _selected_boundary(extra[-1]) if extra else previous
    if remaining > 1e-6:
        extra, remaining = _select_clips(
            candidates=fallback,
            target=remaining,
            used_intervals=used_intervals,
            min_clip=min_clip,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
            fallback=True,
            round_robin=round_robin,
            target_clip=target_clip,
            previous_clip=previous,
            adjacency_diagnostics=adjacency_diagnostics,
            event_id=ref.event_id,
        )
        selected.extend(extra)
        previous = _selected_boundary(extra[-1]) if extra else previous
    if remaining > 1e-6:
        extra, remaining = _select_clips(
            candidates=fallback,
            target=remaining,
            used_intervals=used_intervals,
            min_clip=min_clip,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
            fallback=True,
            allow_reuse=True,
            round_robin=round_robin,
            target_clip=target_clip,
            previous_clip=previous,
            adjacency_diagnostics=adjacency_diagnostics,
            event_id=ref.event_id,
        )
        selected.extend(extra)
    if remaining > 1e-6:
        raise SeriesMatchError(
            f"beat {beat_id} event {ref.event_id} cannot fill its {quota:.3f}s quota with usable story footage"
        )
    if round_robin:
        return selected
    return sorted(selected, key=lambda item: (item.start, item.candidate.shot.index))


def build_edl(
    *,
    beats: list[SeriesReviewBeat],
    timings: list[BeatTiming],
    shots_by_episode: dict[str, list[Shot]],
    min_visual_clip: float,
    max_clip: float,
    min_clip: float = 3.0,
    qa_beats: list[dict[str, object]] | None = None,
    event_bank: SeriesEventBank | None = None,
    clip_profile: str = "legacy",
) -> tuple[list[EdlPlacement], list[str]]:
    if not (0 < min_visual_clip <= min_clip <= max_clip):
        raise SeriesMatchError("clip lengths must satisfy 0 < min_visual_clip <= min_clip <= max_clip")
    if clip_profile not in {"legacy", "dynamic_anime"}:
        raise SeriesMatchError(f"unsupported clip profile: {clip_profile}")
    if clip_profile == "dynamic_anime" and event_bank is None:
        raise SeriesMatchError("--event-bank is required with --clip-profile dynamic_anime")
    events_by_id = {event.event_id: event for event in event_bank.events} if event_bank is not None else {}
    edge_inset_s = DYNAMIC_EDGE_INSET_S if clip_profile == "dynamic_anime" else 0.0
    windows = timing_windows(timings)
    placements: list[EdlPlacement] = []
    warnings: list[str] = []
    for beat in beats:
        if beat.beat_id not in windows:
            raise SeriesMatchError(f"missing timing for beat {beat.beat_id}")
        tl_cursor, tl_end = windows[beat.beat_id]
        beat_duration = tl_end - tl_cursor
        refs_with_shots: list[tuple[SeriesSourceRef, list[Shot]]] = []
        capacities: list[float] = []
        dynamic_rules: list[DynamicClipRule] = []
        for ref in beat.source_refs:
            episode_shots = shots_by_episode.get(ref.episode_key)
            if episode_shots is None:
                raise SeriesMatchError(f"missing shots for episode {ref.episode_key}")
            refs_with_shots.append((ref, episode_shots))
            dynamic_rule: DynamicClipRule | None = None
            if clip_profile == "dynamic_anime":
                event = events_by_id.get(ref.event_id)
                if event is None:
                    raise SeriesMatchError(f"event bank is missing event {ref.event_id}")
                dynamic_rule = _dynamic_rule(beat, event, max_clip=max_clip)
                dynamic_rules.append(dynamic_rule)
            capacities.append(
                _candidate_capacity(
                    fallback_candidates(
                        ref,
                        episode_shots,
                        edge_inset_s=edge_inset_s,
                        inset_min_clip=(dynamic_rule.min_clip if dynamic_rule is not None else min_clip),
                    ),
                    min_visual_clip,
                )
            )
        quotas = _allocate_ref_quotas(
            beat_id=beat.beat_id,
            event_ids=[ref.event_id for ref in beat.source_refs],
            duration=beat_duration,
            capacities=capacities,
            min_clip=min_clip,
            min_visual_clip=min_visual_clip,
            required_floors=[rule.min_clip for rule in dynamic_rules] if dynamic_rules else None,
        )
        used_intervals: dict[str, list[tuple[float, float]]] = {}
        fallback_event_ids: list[str] = []
        short_fallbacks: list[dict[str, object]] = []
        adjacency_fallbacks: list[dict[str, object]] = []
        rules: list[DynamicClipRule | None] = dynamic_rules if dynamic_rules else [None] * len(refs_with_shots)
        for (ref, episode_shots), quota, rule in zip(refs_with_shots, quotas, rules, strict=True):
            effective_min_clip = rule.min_clip if rule is not None else min_clip
            effective_max_clip = rule.max_clip if rule is not None else max_clip
            selected = _ref_plan(
                beat_id=beat.beat_id,
                ref=ref,
                shots=episode_shots,
                quota=quota,
                used_intervals=used_intervals,
                min_clip=effective_min_clip,
                max_clip=effective_max_clip,
                min_visual_clip=min_visual_clip,
                round_robin=rule is not None,
                target_clip=rule.target_clip if rule is not None else None,
                previous_clip=_placement_boundary(placements[-1]) if placements else None,
                adjacency_diagnostics=adjacency_fallbacks,
                edge_inset_s=edge_inset_s,
            )
            if any(item.fallback for item in selected):
                fallback_event_ids.append(ref.event_id)
            for item in selected:
                add_clip(
                    placements,
                    beat_id=beat.beat_id,
                    tl_cursor=tl_cursor,
                    duration=item.duration,
                    candidate=item.candidate,
                    src_start=item.start,
                    reused=item.reused,
                )
                if item.duration + 1e-6 < effective_min_clip:
                    candidate_duration = item.candidate.end - item.candidate.start
                    short_fallbacks.append(
                        {
                            "event_id": ref.event_id,
                            "episode_key": ref.episode_key,
                            "shot_index": item.candidate.shot.index,
                            "duration_s": round(item.duration, 3),
                            "reason": (
                                "candidate_below_min_clip"
                                if candidate_duration + 1e-6 < effective_min_clip
                                else "quota_or_tail_below_min_clip"
                            ),
                            "episode_fallback": item.fallback,
                            "reused": item.reused,
                        }
                    )
                tl_cursor += item.duration
        if fallback_event_ids:
            warnings.append(
                f"beat {beat.beat_id}: used episode-level fallback for " + ", ".join(dict.fromkeys(fallback_event_ids))
            )
        if short_fallbacks:
            warnings.append(f"beat {beat.beat_id}: used {len(short_fallbacks)} clips shorter than --min-clip")
        if adjacency_fallbacks:
            warnings.append(
                f"beat {beat.beat_id}: used {len(adjacency_fallbacks)} unavoidable adjacent source repeats"
            )
        if qa_beats is not None:
            requested = list(dict.fromkeys(ref.event_id for ref in beat.source_refs))
            qa_beats.append(
                {
                    "beat_id": beat.beat_id,
                    "requested_event_ids": requested,
                    "covered_event_ids": requested,
                    "fallback_event_ids": list(dict.fromkeys(fallback_event_ids)),
                    "missing_event_ids": [],
                    "quotas_s": {
                        ref.event_id: round(quota, 3)
                        for ref, quota in zip(beat.source_refs, quotas, strict=True)
                    },
                    "short_fallbacks": short_fallbacks,
                    "short_fallback_diagnostics": short_fallbacks,
                    "clip_profile": clip_profile,
                    "edge_inset_s": edge_inset_s,
                    "event_clip_ranges": {
                        rule.event_id: {
                            "event_type": rule.event_type,
                            "importance": rule.importance,
                            "min_clip_s": rule.min_clip,
                            "max_clip_s": rule.max_clip,
                            "target_clip_s": round(rule.target_clip, 3),
                        }
                        for rule in dynamic_rules
                    },
                    "adjacency_fallbacks": adjacency_fallbacks,
                    "n_adjacent_repeat_fallbacks": sum(
                        1 for item in adjacency_fallbacks if "same_shot" in item["conflicts"]
                    ),
                    "n_contiguous_source_fallbacks": sum(
                        1 for item in adjacency_fallbacks if "contiguous_source" in item["conflicts"]
                    ),
                }
            )
    return validate_edl(placements), warnings


def run_series_match(args: argparse.Namespace) -> int:
    min_clip = float(getattr(args, "min_clip", 3.0))
    if not (0 < args.min_visual_clip <= min_clip <= args.max_clip):
        raise SeriesMatchError("clip lengths must satisfy 0 < min_visual_clip <= min_clip <= max_clip")
    episode_run_dirs = parse_episode_run_dirs(args.episode_run_dir)
    beats = load_series_beats(args.series_review_script.expanduser().resolve())
    timings = load_timings(args.beats_timing.expanduser().resolve())
    clip_profile = str(getattr(args, "clip_profile", "legacy"))
    event_bank_path = getattr(args, "event_bank", None)
    event_bank = load_event_bank(event_bank_path.expanduser().resolve()) if event_bank_path is not None else None
    shots_by_episode = {
        episode_key: load_shots(run_dir / "shots.json")
        for episode_key, run_dir in episode_run_dirs.items()
    }
    qa_beats: list[dict[str, object]] = []
    placements, warnings = build_edl(
        beats=beats,
        timings=timings,
        shots_by_episode=shots_by_episode,
        min_visual_clip=args.min_visual_clip,
        max_clip=args.max_clip,
        min_clip=min_clip,
        qa_beats=qa_beats,
        event_bank=event_bank,
        clip_profile=clip_profile,
    )
    source_map = source_map_from_beats(beats)
    write_json(args.output.expanduser().resolve(), placements)
    write_json(args.output_source_map.expanduser().resolve(), source_map)
    clip_lengths = [placement.tl_end - placement.tl_start for placement in placements]
    meta = EdlMeta(
        total_duration_s=round(placements[-1].tl_end if placements else 0.0, 3),
        n_placements=len(placements),
        n_beats_widened=0,
        n_reused=sum(1 for placement in placements if placement.reused),
        n_speedfit=0,
        n_intro_excluded=0,
        n_empty_beats=0,
        n_high_repeat_beats=0,
        n_dark_fallback_beats=0,
        n_end_credit_excluded=0,
        n_capacity_exhausted_beats=0,
        n_unused_source_reuse=0,
        n_overlapping_repeats=0,
        max_repeat_ratio=0.0,
        avg_clip_len=round(sum(clip_lengths) / len(clip_lengths), 3) if clip_lengths else 0.0,
        coverage_ok=True,
        warnings=warnings,
        seed=0,
        created_at=datetime.now(timezone.utc),
        cache_hits=[],
        algorithm_version=DYNAMIC_ALGORITHM_VERSION if clip_profile == "dynamic_anime" else ALGORITHM_VERSION,
    )
    write_json(args.output.expanduser().resolve().with_name("edl.meta.json"), meta)
    qa_path = args.output_qa.expanduser().resolve() if args.output_qa else args.output.expanduser().resolve().with_name("edl.qa.json")
    write_json(
        qa_path,
        {
            "version": 1,
            "n_beats": len(beats),
            "n_placements": len(placements),
            "source_count": len(source_map.sources),
            "algorithm_version": DYNAMIC_ALGORITHM_VERSION if clip_profile == "dynamic_anime" else ALGORITHM_VERSION,
            "clip_profile": clip_profile,
            "edge_inset_s": DYNAMIC_EDGE_INSET_S if clip_profile == "dynamic_anime" else 0.0,
            "n_episode_fallback_events": sum(len(item["fallback_event_ids"]) for item in qa_beats),
            "n_short_fallbacks": sum(len(item["short_fallbacks"]) for item in qa_beats),
            "n_adjacent_repeat_fallbacks": sum(
                int(item["n_adjacent_repeat_fallbacks"]) for item in qa_beats
            ),
            "n_contiguous_source_fallbacks": sum(
                int(item["n_contiguous_source_fallbacks"]) for item in qa_beats
            ),
            "beats": qa_beats,
            "warnings": warnings,
        },
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_series_match(args)
    except (SeriesMatchError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"series_match: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
