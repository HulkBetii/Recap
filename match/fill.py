from __future__ import annotations

from dataclasses import dataclass

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, Shot
from match.candidates import candidates_for_window, widen_until_enough
from match.scoring import ScoringWeights, rank_shots


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
        ranked = rank_shots(available, reuse_counts, weights)
        shot = ranked[0]
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
        reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
        used_in_beat.add(shot.index)
        remaining = round(remaining - clip_len, 6)

    if remaining > 0.02:
        if allow_repeat and candidates:
            warnings.append(f"beat {beat.beat_id} required controlled repeat fallback")
            while remaining > 1e-6:
                shot = rank_shots(candidates, reuse_counts, weights)[0]
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
                reuse_counts[shot.index] = reuse_counts.get(shot.index, 0) + 1
                remaining = round(remaining - clip_len, 6)
        elif allow_speedfit and fragments:
            warnings.append(f"beat {beat.beat_id} would require speedfit; not applied to existing fragments")
            speedfit_count += 1
        else:
            warnings.append(f"beat {beat.beat_id} could not fill {remaining:.3f}s")

    fragments = trim_fragments_to_duration(sorted(fragments, key=lambda item: (item.src_in, item.src_out)), timing.duration)
    return FillResult(
        fragments=fragments,
        widened=widen_count > 0,
        reused_count=reuse_count,
        speedfit_count=speedfit_count,
        warnings=warnings,
    )


def trim_fragments_to_duration(fragments: list[Fragment], target_duration: float) -> list[Fragment]:
    output: list[Fragment] = []
    remaining = target_duration
    for fragment in fragments:
        if remaining <= 1e-6:
            break
        duration = min(fragment.duration, remaining)
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
    src_in = max(previous.src_in, src_out - duration)
    if src_out <= src_in + 1e-6:
        src_in = previous.src_in
        src_out = min(previous.src_out, previous.src_in + max(duration, 0.001))
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
