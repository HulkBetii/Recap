"""Sentence-anchored footage (G5 `--sentence-anchors`).

Each beat narrates several moments of its source span. With `review_script.sentences.json` (G2: which
film-map segments every sentence tells) and `sentence_timing.json` (G3: when every sentence is spoken), the
beat is filled sentence by sentence: the footage under a sentence comes from that sentence's own segments,
instead of the beat's span being spread linearly over the beat's audio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from common.schema import BeatTiming, EdlPlacement, ReviewBeat, SentenceRef, SentenceTimestamp, Shot
from common.text import split_sentences
from match.candidates import is_dark_fallback_candidate
from match.fill import FillResult, assign_timeline, fill_beat

# A sentence's source window is widened to at least this multiple of its audio so there is footage to pick.
MIN_SOURCE_AUDIO_RATIO = 1.5
MIN_SOURCE_WINDOW_S = 4.0
# Footage within this distance of the sentence's own segments still counts as on target in QA.
REF_HIT_TOLERANCE_S = 2.0


@dataclass(frozen=True)
class SentenceWindow:
    idx: int
    timing: BeatTiming
    src_start: float
    src_end: float
    grounded: bool
    ref_start: float | None
    ref_end: float | None


def _widen(start: float, end: float, needed: float, lower: float, upper: float) -> tuple[float, float]:
    """Grow [start, end] evenly to `needed` seconds without leaving [lower, upper]."""
    if upper - lower <= needed:
        return lower, upper
    missing = needed - (end - start)
    if missing <= 0:
        return start, end
    start, end = start - missing / 2, end + missing / 2
    if start < lower:
        end += lower - start
        start = lower
    if end > upper:
        start -= end - upper
        end = upper
    return max(lower, start), min(upper, end)


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
    lower, upper = beat.src_tc_start, beat.src_tc_end
    windows: list[SentenceWindow] = []
    for position, (ref, stamp) in enumerate(zip(refs, stamps)):
        duration = stamp.tl_end - stamp.tl_start
        if ref.grounded:
            start, end = float(ref.src_tc_start), float(ref.src_tc_end)  # type: ignore[arg-type]
        else:
            # Commentary sentence: keep the footage between the neighbouring sentences' moments.
            previous = next((item for item in reversed(refs[:position]) if item.grounded), None)
            following = next((item for item in refs[position + 1 :] if item.grounded), None)
            start = float(previous.src_tc_end) if previous else lower  # type: ignore[arg-type]
            end = float(following.src_tc_start) if following else upper  # type: ignore[arg-type]
            if end <= start:
                start, end = min(start, end), max(start, end) + 0.001
        start, end = max(lower, min(start, upper)), max(lower, min(end, upper))
        start, end = _widen(start, max(end, start + 0.001), max(MIN_SOURCE_WINDOW_S, MIN_SOURCE_AUDIO_RATIO * duration), lower, upper)
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
                src_start=round(start, 3),
                src_end=round(end, 3),
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


def unused_source_intervals(
    window: SentenceWindow,
    used: Sequence[tuple[float, float]],
    *,
    lower: float,
    upper: float,
    min_length: float,
) -> list[tuple[float, float]] | None:
    """Unused source inside the sentence window, grown forward then backward (within the beat span) until it
    holds the sentence's audio. None when the beat span has no such room left; the caller then allows repeats."""
    needed = window.timing.duration * MIN_SOURCE_AUDIO_RATIO
    start, end = window.src_start, window.src_end
    for _ in range(4):
        free = [(a, b) for a, b in subtract_intervals(start, end, used) if b - a >= min_length]
        if sum(b - a for a, b in free) >= needed:
            return [(round(a, 3), round(b, 3)) for a, b in free]
        if start <= lower and end >= upper:
            break
        missing = needed - sum(b - a for a, b in free) + 1.0
        if end < upper:
            end = min(upper, end + missing)
        else:
            start = max(lower, start - missing)
    return None


def fill_beat_by_sentences(
    *,
    beat: ReviewBeat,
    timing: BeatTiming,
    windows: Sequence[SentenceWindow],
    reuse_counts: dict[int, int],
    fill_kwargs: dict[str, Any],
    used_sources: Sequence[EdlPlacement] | None = None,
) -> tuple[FillResult, list[EdlPlacement], list[dict[str, Any]]]:
    """Fill every sentence window with `fill_beat` and merge the results into one beat-level result."""
    placements: list[EdlPlacement] = []
    results: list[FillResult] = []
    diagnostics: list[dict[str, Any]] = []
    shots: list[Shot] = fill_kwargs["shots"]
    min_visual_clip = float(fill_kwargs.get("min_visual_clip", 0.0))
    used = [(placement.src_in, placement.src_out) for placement in (used_sources or [])]
    for window in windows:
        # Neighbouring sentences often cite the same segments; footage already on screen is skipped so the
        # next sentence continues from unused source instead of repeating it.
        intervals = unused_source_intervals(
            window,
            used,
            lower=beat.src_tc_start,
            upper=beat.src_tc_end,
            min_length=max(min_visual_clip, 0.5),
        )
        restrict: dict[str, Any] = {}
        if intervals is not None:
            ids = {shot.index for shot in shots if any(shot.tc_start < end and start < shot.tc_end for start, end in intervals)}
            restrict = {
                "candidate_filter_ids": ids,
                "dark_candidate_ids": {
                    shot.index
                    for shot in shots
                    if shot.index in ids
                    and fill_kwargs.get("allow_dark_fallback", True)
                    and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip)
                },
                "source_intervals": intervals,
            }
        span = (intervals[0][0], intervals[-1][1]) if intervals is not None else (window.src_start, window.src_end)
        sentence_beat = beat.model_copy(update={"src_tc_start": span[0], "src_tc_end": span[1]})
        result = fill_beat(beat=sentence_beat, timing=window.timing, reuse_counts=reuse_counts, **{**fill_kwargs, **restrict})
        sentence_placements = assign_timeline(result.fragments, window.timing)
        results.append(result)
        placements.extend(sentence_placements)
        used.extend((placement.src_in, placement.src_out) for placement in sentence_placements)
        diagnostic: dict[str, Any] = {
            "sentence_idx": window.idx,
            "replaced": True,
            "tl_start": window.timing.tl_start,
            "tl_end": window.timing.tl_end,
            "source_window": [window.src_start, window.src_end],
            "grounded": window.grounded,
            "ref_window": [window.ref_start, window.ref_end] if window.grounded else None,
            "n_placements": len(sentence_placements),
            "widen_count": result.widen_count,
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
