from __future__ import annotations

from dataclasses import dataclass

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
    max_repeat_per_beat: int = 2,
    max_repeat_ratio_per_beat: float = 0.35,
    min_repeat_alternative_score_ratio: float = 0.75,
    adjacent_shot_repeat_penalty: float = 0.50,
    ordered_fill: bool = False,
) -> FillResult:
    warnings: list[str] = []
    window_start, window_end, candidates, widen_count = widen_until_enough(
        shots=shots,
        start=beat.src_tc_start,
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
        ranked = rank_shots(available, reuse_counts, weights, semantic_scores, beat.beat_id)
        if ordered_fill:
            ranked = rank_for_ordered_fill(ranked, reuse_counts, weights, semantic_scores or {}, beat.beat_id)
        shot = choose_diverse_shot(
            ranked,
            reuse_counts,
            weights,
            semantic_scores or {},
            beat.beat_id,
            previous_shot_index,
            min_repeat_alternative_score_ratio,
            adjacent_shot_repeat_penalty,
        )
        src_start = max(window_start, shot.tc_start)
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

    if remaining > 0.02:
        if allow_repeat and candidates:
            warnings.append(f"beat {beat.beat_id} required controlled repeat fallback")
            while remaining > 1e-6:
                ranked_repeat = [shot for shot in rank_shots(candidates, reuse_counts, weights, semantic_scores, beat.beat_id) if repeat_by_shot.get(shot.index, 0) < max_repeat_per_beat]
                if ordered_fill:
                    ranked_repeat = rank_for_ordered_fill(ranked_repeat, reuse_counts, weights, semantic_scores or {}, beat.beat_id)
                if not ranked_repeat:
                    ranked_repeat = rank_shots(candidates, reuse_counts, weights, semantic_scores, beat.beat_id)
                    if ordered_fill:
                        ranked_repeat = rank_for_ordered_fill(ranked_repeat, reuse_counts, weights, semantic_scores or {}, beat.beat_id)
                    warnings.append(f"beat {beat.beat_id} exceeded repeat cap during fallback")
                shot = choose_diverse_shot(
                    ranked_repeat,
                    reuse_counts,
                    weights,
                    semantic_scores or {},
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

    fragments = trim_fragments_to_duration(sorted(fragments, key=lambda item: (item.src_in, item.src_out)), timing.duration)
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
    beat_id: int,
) -> list[Shot]:
    return sorted(
        ranked,
        key=lambda shot: (
            shot.tc_start,
            -score_shot(shot, reuse_counts.get(shot.index, 0), weights, semantic_scores.get((beat_id, shot.index), 0.0)),
            shot.index,
        ),
    )

def choose_diverse_shot(
    ranked: list[Shot],
    reuse_counts: dict[int, int],
    weights: ScoringWeights,
    semantic_scores: dict[tuple[int, int], float],
    beat_id: int,
    previous_shot_index: int | None,
    min_alternative_ratio: float,
    adjacent_penalty: float,
) -> Shot:
    if not ranked:
        raise ValueError("cannot choose from empty candidates")
    top = ranked[0]
    top_score = score_shot(top, reuse_counts.get(top.index, 0), weights, semantic_scores.get((beat_id, top.index), 0.0))
    if previous_shot_index is None or top.index != previous_shot_index:
        return top
    threshold = max(top_score * min_alternative_ratio, top_score - adjacent_penalty)
    for candidate in ranked[1:]:
        candidate_score = score_shot(
            candidate,
            reuse_counts.get(candidate.index, 0),
            weights,
            semantic_scores.get((beat_id, candidate.index), 0.0),
        )
        if candidate.index != previous_shot_index and candidate_score >= threshold:
            return candidate
    return top

def trim_fragments_to_duration(fragments: list[Fragment], target_duration: float) -> list[Fragment]:
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


def fill_timeline_gaps(placements: list[EdlPlacement], total_duration: float) -> list[EdlPlacement]:
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id))
    if not ordered:
        return ordered
    output: list[EdlPlacement] = []
    previous: EdlPlacement | None = None
    for placement in ordered:
        if previous is not None and placement.tl_start > previous.tl_end + 1e-3:
            gap = round(placement.tl_start - previous.tl_end, 3)
            output.append(make_pause_filler(previous, previous.tl_end, placement.tl_start, gap))
        output.append(placement)
        previous = placement
    if previous is not None and total_duration > previous.tl_end + 1e-3:
        gap = round(total_duration - previous.tl_end, 3)
        output.append(make_pause_filler(previous, previous.tl_end, total_duration, gap))
    return output

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
