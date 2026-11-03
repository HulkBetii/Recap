"""Sentence-anchored footage (G5 `--sentence-anchors`).

Each beat narrates several moments of its source span. With `review_script.sentences.json` (G2: which
film-map segments every sentence tells) and `sentence_timing.json` (G3: when every sentence is spoken), the
beat is filled sentence by sentence: the footage under a sentence comes from that sentence's own segments,
instead of the beat's span being spread linearly over the beat's audio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, SentenceRef, SentenceTimestamp, Shot
from common.sentence_windows import MIN_SOURCE_AUDIO_RATIO, sentence_source_windows
from common.text import split_sentences
from match.candidates import is_dark_fallback_candidate
from match.fill import FillResult, assign_timeline, fill_beat

# Footage within this distance of the sentence's own segments still counts as on target in QA.
REF_HIT_TOLERANCE_S = 2.0
# Passes per sentence: later passes fill what an earlier one left without footage, so no hole reaches the timeline
# gap filler (which replays the previous clip's tail or slows one shot to a fraction of its speed).
SENTENCE_FILL_PASSES = 3
# Pools prefer unused pieces of at least this share of min_clip. On `Dem Thanh` (v8 A/B, same audio) 0.5 cut
# clips under 1.5 s from 210 to 137 for 0.848 -> 0.838 on-ref screen time; 0.67 dropped on-ref to 0.817.
PREFERRED_PIECE_CLIP_RATIO = 0.5


@dataclass(frozen=True)
class SentenceWindow:
    idx: int
    timing: BeatTiming
    src_start: float
    src_end: float
    grounded: bool
    ref_start: float | None
    ref_end: float | None


def plan_sentence_windows(
    beat: ReviewBeat,
    timing: BeatTiming,
    refs: Sequence[SentenceRef],
    stamps: Sequence[SentenceTimestamp],
) -> tuple[list[SentenceWindow] | None, str | None]:
    """Source/timeline window of every sentence, or (None, reason) when the beat must use beat-level matching."""
    n_sentences = len(split_sentences(beat.narration))
    if not refs or len(refs) != n_sentences or len(stamps) != n_sentences:
        return None, f"sentence data does not match the narration ({len(refs)} refs, {len(stamps)} timings, {n_sentences} sentences)"
    if not any(ref.grounded for ref in refs):
        return None, "no sentence is linked to source segments"
    spans = [(float(ref.src_tc_start), float(ref.src_tc_end)) if ref.grounded else None for ref in refs]  # type: ignore[arg-type]
    durations = [stamp.tl_end - stamp.tl_start for stamp in stamps]
    sources = sentence_source_windows(spans, durations, beat.src_tc_start, beat.src_tc_end)
    windows: list[SentenceWindow] = []
    for ref, stamp, (start, end) in zip(refs, stamps, sources):
        windows.append(
            SentenceWindow(
                idx=ref.idx,
                timing=BeatTiming(
                    beat_id=beat.beat_id,
                    audio_path=timing.audio_path,
                    tl_start=stamp.tl_start,
                    tl_end=stamp.tl_end,
                    duration=round(stamp.tl_end - stamp.tl_start, 6),
                ),
                src_start=start,
                src_end=end,
                grounded=ref.grounded,
                ref_start=ref.src_tc_start,
                ref_end=ref.src_tc_end,
            )
        )
    return windows, None


def ref_hit_ratio(placements: Sequence[EdlPlacement], start: float, end: float) -> float:
    """Share of the placements' screen time whose source lies within the sentence's own segments."""
    total = sum(placement.tl_end - placement.tl_start for placement in placements)
    if total <= 0:
        return 0.0
    hit = 0.0
    for placement in placements:
        source = placement.src_out - placement.src_in
        overlap = min(placement.src_out, end + REF_HIT_TOLERANCE_S) - max(placement.src_in, start - REF_HIT_TOLERANCE_S)
        if source > 0 and overlap > 0:
            hit += (placement.tl_end - placement.tl_start) * min(1.0, overlap / source)
    return hit / total


def subtract_intervals(start: float, end: float, used: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    free = [(start, end)]
    for used_start, used_end in used:
        next_free: list[tuple[float, float]] = []
        for free_start, free_end in free:
            if used_end <= free_start or used_start >= free_end:
                next_free.append((free_start, free_end))
                continue
            if used_start > free_start:
                next_free.append((free_start, used_start))
            if used_end < free_end:
                next_free.append((used_end, free_end))
        free = next_free
    return free


def free_shot_source(
    shots: Sequence[Shot],
    start: float,
    end: float,
    used: Sequence[tuple[float, float]],
    min_length: float,
) -> list[tuple[float, float]]:
    """Source of `shots` inside [start, end] that is not on screen yet, in pieces of at least `min_length`."""
    pieces: list[tuple[float, float]] = []
    for shot in shots:
        piece_start, piece_end = max(start, shot.tc_start), min(end, shot.tc_end)
        if piece_end - piece_start >= min_length:
            pieces.extend(piece for piece in subtract_intervals(piece_start, piece_end, used) if piece[1] - piece[0] >= min_length)
    return sorted(pieces)


def unused_source_intervals(
    window: SentenceWindow,
    used: Sequence[tuple[float, float]],
    *,
    tiers: Sequence[Sequence[Shot]],
    bounds: Sequence[tuple[float, float]],
    min_length: float,
    reserved: Sequence[tuple[float, float]] = (),
    preferred_length: float = 0.0,
) -> list[tuple[float, float]] | None:
    """Unused footage of pickable shots around the sentence window, grown until it holds 1.5x the sentence's audio.

    Only the source of shots the fill may pick counts: a window over shots excluded for overlay text once kept
    a 0.6 s sliver of usable footage and replayed it eight times. Growth goes forward near the window, then
    backward near it, then evenly on both sides out to the bound. Attempts, in order: the beat span in pieces of
    at least `preferred_length` (leftover slivers otherwise cut the footage into sub-1.5 s jumps), the beat span
    in pieces of `min_length`, then the beat span widened like the beat-level fill. Each attempt first leaves
    `reserved` (the moments later sentences of the beat tell) alone, since sentences over an excluded stretch
    otherwise took the next sentence's own footage, then tries usable shots before dark fallback shots. An
    attempt that cannot reach 1.5x is still taken when it holds the audio once. None when none does; the
    caller's fill then repeats footage.
    """
    duration = window.timing.duration
    needed = duration * MIN_SOURCE_AUDIO_RATIO
    blocks = [[*used, *reserved], list(used)] if reserved else [list(used)]
    attempts = [(bounds[0], blocked, preferred_length) for blocked in blocks] if preferred_length > min_length else []
    attempts += [(bound, blocked, min_length) for bound in bounds for blocked in blocks]
    for (lower, upper), blocked, piece in attempts:
        near_lower, near_upper = max(lower, window.src_start - needed), min(upper, window.src_end + needed)
        limits = [(window.src_start, near_upper, False), (near_lower, near_upper, False), (lower, upper, True)]
        for tier in tiers:
            nearby = [shot for shot in tier if shot.tc_end > lower and shot.tc_start < upper]
            start, end = window.src_start, window.src_end
            free: list[tuple[float, float]] = []
            for limit_lower, limit_upper, evenly in limits:
                while True:
                    free = free_shot_source(nearby, start, end, blocked, piece)
                    total = sum(b - a for a, b in free)
                    if total >= needed:
                        return [(round(a, 3), round(b, 3)) for a, b in free]
                    if start <= limit_lower and end >= limit_upper:
                        break
                    missing = needed - total + 1.0
                    if evenly and start > limit_lower and end < limit_upper:
                        start, end = max(limit_lower, start - missing / 2), min(limit_upper, end + missing / 2)
                    elif end < limit_upper:
                        end = min(limit_upper, end + missing)
                    else:
                        start = max(limit_lower, start - missing)
            if free and sum(b - a for a, b in free) >= duration:
                return [(round(a, 3), round(b, 3)) for a, b in free]
    return None


def source_restriction(
    intervals: list[tuple[float, float]],
    shots: Sequence[Shot],
    *,
    allow_dark_fallback: bool,
    min_visual_clip: float,
) -> dict[str, Any]:
    """`fill_beat` arguments that keep the fill inside `intervals`."""
    ids = {shot.index for shot in shots if any(shot.tc_start < end and start < shot.tc_end for start, end in intervals)}
    return {
        "candidate_filter_ids": ids,
        "dark_candidate_ids": {
            shot.index
            for shot in shots
            if shot.index in ids and allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)
        },
        "source_intervals": intervals,
    }


def fill_beat_by_sentences(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    windows: Sequence[SentenceWindow],
    reuse_counts: dict[int, int],
    fill_kwargs: dict[str, Any],
    used_sources: Sequence[EdlPlacement] | None = None,
    sentence_visual_scores: dict[tuple[int, int], dict[int, float]] | None = None,
    visual_weight: float = 0.0,
) -> tuple[FillResult, list[EdlPlacement], list[dict[str, Any]]]:
    """Fill every sentence window with `fill_beat` and merge the results into one beat-level result."""
    placements: list[EdlPlacement] = []
    results: list[FillResult] = []
    diagnostics: list[dict[str, Any]] = []
    shots: list[Shot] = fill_kwargs["shots"]
    min_visual_clip = float(fill_kwargs.get("min_visual_clip", 0.0))
    allow_dark_fallback = bool(fill_kwargs.get("allow_dark_fallback", True))
    min_length = max(min_visual_clip, 0.5)
    usable = [shot for shot in shots if shot.is_usable]
    dark = [shot for shot in shots if allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)]
    tiers = [usable, usable + dark] if dark else [usable]
    widen = float(fill_kwargs.get("widen_margin", 0.0)) * int(fill_kwargs.get("max_widen", 0))
    bounds = [(beat.src_tc_start, beat.src_tc_end)]
    if widen > 0:
        bounds.append((max(0.0, beat.src_tc_start - widen), beat.src_tc_end + widen))
    preferred_length = max(min_length, PREFERRED_PIECE_CLIP_RATIO * float(fill_kwargs.get("min_clip", 0.0)))
    used = [(placement.src_in, placement.src_out) for placement in (used_sources or [])]
    for position, window in enumerate(windows):
        # Later sentences' own moments, minus this sentence's window (sentences often share segments).
        reserved = [
            piece
            for later in windows[position + 1 :]
            if later.grounded and later.ref_start is not None and later.ref_end is not None
            for piece in subtract_intervals(later.ref_start, later.ref_end, [(window.src_start, window.src_end)])
        ]
        scoring: dict[str, Any] = {}
        visual = (sentence_visual_scores or {}).get((beat.beat_id, window.idx))
        if visual and visual_weight > 0:
            # SigLIP scores for this sentence's own query replace the beat-level visual scores.
            scoring["visual_scores"] = {(beat.beat_id, shot_index): score for shot_index, score in visual.items()}
            scoring["weights"] = replace(fill_kwargs["weights"], visual=visual_weight)
            # The window is already the right moment: put every shot in it in the same drift tier, otherwise
            # chronological ranking (tier before score) never reaches a well-scored shot past max_source_drift_s.
            scoring["max_source_drift_s"] = max(float(fill_kwargs.get("max_source_drift_s", 12.0)), window.src_end - window.src_start)
        sentence_placements: list[EdlPlacement] = []
        sentence_results: list[FillResult] = []
        target = window.timing
        for fill_pass in range(SENTENCE_FILL_PASSES):
            # Neighbouring sentences often cite the same segments; footage already on screen is skipped so the
            # next sentence continues from unused source instead of repeating it.
            intervals = unused_source_intervals(
                replace(window, timing=target),
                used,
                tiers=tiers,
                bounds=bounds,
                min_length=min_length,
                reserved=reserved,
                preferred_length=preferred_length,
            )
            restrict: dict[str, Any] = {}
            if intervals is not None:
                restrict = source_restriction(intervals, shots, allow_dark_fallback=allow_dark_fallback, min_visual_clip=min_visual_clip)
                span = (intervals[0][0], intervals[-1][1])
            elif fill_pass == 0:
                span = (window.src_start, window.src_end)
            else:
                span = (beat.src_tc_start, beat.src_tc_end)
            sentence_beat = beat.model_copy(update={"src_tc_start": span[0], "src_tc_end": span[1]})
            result = fill_beat(beat=sentence_beat, timing=target, reuse_counts=reuse_counts, **{**fill_kwargs, **restrict, **scoring})
            pass_placements = assign_timeline(result.fragments, target)
            sentence_results.append(result)
            sentence_placements.extend(pass_placements)
            used.extend((placement.src_in, placement.src_out) for placement in pass_placements)
            filled_to = pass_placements[-1].tl_end if pass_placements else target.tl_start
            rest = round(window.timing.tl_end - filled_to, 6)
            # A rest shorter than a clip is absorbed by the neighbouring placements; one that fits a clip is not,
            # and the gap filler would cover it by replaying the end of the clip before it.
            if fill_pass == SENTENCE_FILL_PASSES - 1 or rest <= 0.05 or rest + 1e-6 < min_visual_clip:
                break
            target = BeatTiming(beat_id=beat.beat_id, audio_path=timing.audio_path, tl_start=filled_to, tl_end=window.timing.tl_end, duration=rest)
        for earlier in sentence_results[:-1]:
            # The next pass filled what this one left; only the last pass's shortfall is real.
            earlier.warnings[:] = [warning for warning in earlier.warnings if not warning.startswith(f"beat {beat.beat_id} could not fill")]
        results.extend(sentence_results)
        placements.extend(sentence_placements)
        diagnostic: dict[str, Any] = {
            "sentence_idx": window.idx,
            "replaced": True,
            "tl_start": window.timing.tl_start,
            "tl_end": window.timing.tl_end,
            "source_window": [window.src_start, window.src_end],
            "grounded": window.grounded,
            "ref_window": [window.ref_start, window.ref_end] if window.grounded else None,
            "n_placements": len(sentence_placements),
            "visual_scored": bool(visual and visual_weight > 0),
            "visual_score_avg": (
                round(sum(visual.get(p.shot_index, 0.0) for p in sentence_placements) / len(sentence_placements), 3)
                if visual and sentence_placements
                else None
            ),
            "widen_count": max(item.widen_count for item in sentence_results),
            "fill_passes": len(sentence_results),
        }
        if window.grounded:
            diagnostic["ref_hit_ratio"] = round(ref_hit_ratio(sentence_placements, window.ref_start, window.ref_end), 3)  # type: ignore[arg-type]
        diagnostics.append(diagnostic)
    merged = FillResult(
        fragments=[fragment for result in results for fragment in result.fragments],
        widened=any(result.widened for result in results),
        reused_count=sum(result.reused_count for result in results),
        speedfit_count=sum(result.speedfit_count for result in results),
        warnings=[warning for result in results for warning in result.warnings],
        candidate_shot_ids=list(dict.fromkeys(shot for result in results for shot in result.candidate_shot_ids)),
        window_start=min(result.window_start for result in results),
        window_end=max(result.window_end for result in results),
        source_cursor_start=results[0].source_cursor_start,
        widen_count=max(result.widen_count for result in results),
        required_duration_s=timing.duration,
        primary_capacity_s=sum(result.primary_capacity_s for result in results),
        dark_capacity_s=sum(result.dark_capacity_s for result in results),
        total_capacity_s=sum(result.total_capacity_s for result in results),
        capacity_exhausted=any(result.capacity_exhausted for result in results),
        dark_candidate_ids=list(dict.fromkeys(shot for result in results for shot in result.dark_candidate_ids)),
        dark_selected_ids=list(dict.fromkeys(shot for result in results for shot in result.dark_selected_ids)),
        unused_source_reuse_count=sum(result.unused_source_reuse_count for result in results),
        overlapping_repeat_count=sum(result.overlapping_repeat_count for result in results),
        source_intervals=[(window.src_start, window.src_end) for window in windows],
        source_interval_weights=[window.timing.duration for window in windows],
    )
    return merged, placements, diagnostics
