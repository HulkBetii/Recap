from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.candidates import candidates_for_window, widen_until_enough
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
) -> FillResult:
    warnings: list[str] = []
    effective_start = max(beat.src_tc_start, source_start_override) if source_start_override is not None else beat.src_tc_start
    window_start, window_end, candidates, widen_count = widen_until_enough(
        shots=shots,
        start=effective_start,
        end=beat.src_tc_end,
        needed_duration=timing.duration,
        margin=widen_margin,
        max_widen=max_widen,
    )
    if widen_count > 0:
        warnings.append(f"beat {beat.beat_id} widened source window {widen_count} time(s)")
    fragments: list[Fragment] = []
    remaining = timing.duration
    used_in_beat: set[int] = set()
    reuse_count = 0
    speedfit_count = 0
    repeat_by_shot: dict[int, int] = {}
    previous_shot_index: int | None = None
    source_cursor = window_start
    source_span = max(0.001, window_end - window_start)

    while remaining > 1e-6:
        available = [shot for shot in candidates if shot.index not in used_in_beat]
        repeated = False
        if not available:
            if not allow_repeat:
                break
            available = candidates
            repeated = True
        if not available:
            break
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
        )
        src_start = max(window_start, shot.tc_start)
        if match_strategy == "chronological" and shot.tc_start < source_cursor < shot.tc_end:
            src_start = max(src_start, source_cursor)
        src_end = min(window_end, shot.tc_end)
        usable_len = max(0.0, src_end - src_start)
        if usable_len <= 0:
            used_in_beat.add(shot.index)
            continue
        clip_len = min(max_clip, usable_len, remaining)
        if clip_len < min_clip and remaining > min_clip and usable_len >= min_clip:
            clip_len = min(min_clip, remaining)
        if clip_len <= 0:
            break
        fragments.append(
            Fragment(
                src=shot.src,
                src_in=round(src_start, 3),
                src_out=round(src_start + clip_len, 3),
                beat_id=beat.beat_id,
                shot_index=shot.index,
                reused=repeated or reuse_counts.get(shot.index, 0) > 0,
            )
        )
        if repeated or reuse_counts.get(shot.index, 0) > 0:
            reuse_count += 1
            repeat_by_shot[shot.index] = repeat_by_shot.get(shot.index, 0) + 1
        reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
        previous_shot_index = shot.index
        used_in_beat.add(shot.index)
        remaining = round(remaining - clip_len, 6)
        if ordered_fill_by_audio_progress and timing.duration > 0:
            progress = min(1.0, max(0.0, (timing.duration - remaining) / timing.duration))
            source_cursor = min(window_end, window_start + source_span * progress)

    if remaining > 0.02:
        if allow_repeat and candidates:
            warnings.append(f"beat {beat.beat_id} required controlled repeat fallback")
            while remaining > 1e-6:
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
                        )
                    warnings.append(f"beat {beat.beat_id} exceeded repeat cap during fallback")
                shot = choose_diverse_shot(
                    ranked_repeat,
                    reuse_counts,
                    weights,
                    semantic_scores or {},
                    visual_scores or {},
                    beat.beat_id,
                    previous_shot_index,
                    min_repeat_alternative_score_ratio,
                    adjacent_shot_repeat_penalty,
                )
                clip_len = min(max_clip, max(0.05, remaining), shot.duration)
                src_start = shot.tc_start
                fragments.append(
                    Fragment(
                        src=shot.src,
                        src_in=round(src_start, 3),
                        src_out=round(src_start + clip_len, 3),
                        beat_id=beat.beat_id,
                        shot_index=shot.index,
                        reused=True,
                    )
                )
                reuse_count += 1
                repeat_by_shot[shot.index] = repeat_by_shot.get(shot.index, 0) + 1
                reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
                previous_shot_index = shot.index
                remaining = round(remaining - clip_len, 6)
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

    fragments = trim_fragments_to_duration(
        sorted(fragments, key=lambda item: (item.src_in, item.src_out)),
        timing.duration,
        min_visual_clip=min_visual_clip,
    )
    return FillResult(
        fragments=fragments,
        widened=widen_count > 0,
        reused_count=reuse_count,
        speedfit_count=speedfit_count,
        warnings=warnings,
    )



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
) -> list[Shot]:
    def drift(shot: Shot) -> float:
        if shot.tc_start <= source_cursor <= shot.tc_end:
            return 0.0
        return min(abs(shot.tc_start - source_cursor), abs(shot.tc_end - source_cursor))

    def sort_key(shot: Shot) -> tuple[float, float, float, int]:
        is_before_cursor = shot.tc_end < source_cursor
        distance = drift(shot)
        score = score_shot(
            shot,
            reuse_counts.get(shot.index, 0),
            weights,
            semantic_scores.get((beat_id, shot.index), 0.0),
            visual_scores.get((beat_id, shot.index), 0.0),
        )
        if match_strategy == "chronological":
            beyond_drift = distance > max_source_drift_s
            return (1.0 if beyond_drift else 0.0, distance, -score, shot.index)
        if match_strategy == "hybrid":
            drift_penalty = chronology_weight * min(1.0, distance / max(max_source_drift_s, 0.001))
            return (1 if is_before_cursor else 0, -(score - drift_penalty), distance, shot.index)
        return (0.0, -score, distance, shot.index)

    return sorted(ranked, key=sort_key)

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
    for candidate in ranked[1:]:
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
        duration = fragment.duration
        if index > 0:
            previous = output[index - 1]
            output[index - 1] = replace(previous, src_out=round(previous.src_out + duration, 3))
            del output[index]
            index = max(0, index - 1)
            continue
        next_fragment = output[index + 1]
        output[index + 1] = replace(next_fragment, src_out=round(next_fragment.src_out + duration, 3))
        del output[index]
    total = sum(fragment.duration for fragment in output)
    diff = round(target_duration - total, 6)
    if output and abs(diff) > 1e-6 and output[-1].duration + diff > 0:
        output[-1] = replace(output[-1], src_out=round(output[-1].src_out + diff, 3))
    return output


def assign_timeline(fragments: list[Fragment], timing: BeatTiming) -> list[EdlPlacement]:
    placements: list[EdlPlacement] = []
    cursor = timing.tl_start
    for index, fragment in enumerate(fragments):
        duration = fragment.duration
        if index == len(fragments) - 1:
            tl_end = timing.tl_end
        else:
            tl_end = round(cursor + duration, 3)
        placements.append(
            EdlPlacement(
                tl_start=round(cursor, 3),
                tl_end=round(tl_end, 3),
                src=fragment.src,
                src_in=fragment.src_in,
                src_out=round(fragment.src_in + (tl_end - cursor), 3),
                beat_id=fragment.beat_id,
                shot_index=fragment.shot_index,
                reused=fragment.reused,
                speed=fragment.speed,
            )
        )
        cursor = tl_end
    return placements


def fill_timeline_gaps(placements: list[EdlPlacement], total_duration: float, *, min_visual_clip: float = 0.0) -> list[EdlPlacement]:
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id))
    if not ordered:
        return ordered
    output: list[EdlPlacement] = []
    previous: EdlPlacement | None = None
    for placement in ordered:
        if previous is not None and placement.tl_start > previous.tl_end + 1e-3:
            gap = round(placement.tl_start - previous.tl_end, 3)
            if min_visual_clip > 0 and gap < min_visual_clip and output:
                output[-1] = extend_placement(output[-1], placement.tl_start)
                previous = output[-1]
            else:
                output.append(make_pause_filler(previous, previous.tl_end, placement.tl_start, gap))
        output.append(placement)
        previous = placement
    if previous is not None and total_duration > previous.tl_end + 1e-3:
        gap = round(total_duration - previous.tl_end, 3)
        if min_visual_clip > 0 and gap < min_visual_clip and output:
            output[-1] = extend_placement(output[-1], total_duration)
        else:
            output.append(make_pause_filler(previous, previous.tl_end, total_duration, gap))
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
                src_out = round(src_cursor + actual_duration / max(placement.speed, 0.001), 3)
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
        "src_out": round(placement.src_out + extension / max(placement.speed, 0.001), 3),
    })

def make_pause_filler(previous: EdlPlacement, tl_start: float, tl_end: float, duration: float) -> EdlPlacement:
    src_out = previous.src_out
    src_in = src_out - duration
    if src_in < 0:
        src_in = previous.src_in
        src_out = src_in + max(duration, 0.001)
    if src_out <= src_in + 1e-6:
        src_in = previous.src_in
        src_out = src_in + max(duration, 0.001)
    return EdlPlacement(
        tl_start=round(tl_start, 3),
        tl_end=round(tl_end, 3),
        src=previous.src,
        src_in=round(src_in, 3),
        src_out=round(src_out, 3),
        beat_id=previous.beat_id,
        shot_index=previous.shot_index,
        reused=True,
        speed=1.0,
    )
