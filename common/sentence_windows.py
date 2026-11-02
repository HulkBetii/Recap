"""Source window of each narration sentence, shared by sentence-anchored matching (G5, series match) and
VLM shot picking, so every stage looks at the same footage for a sentence."""

from __future__ import annotations

from collections.abc import Sequence

# A sentence's source window is widened to at least this multiple of its audio so there is footage to pick.
MIN_SOURCE_AUDIO_RATIO = 1.5
MIN_SOURCE_WINDOW_S = 4.0


def widen_window(start: float, end: float, needed: float, lower: float, upper: float) -> tuple[float, float]:
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


def sentence_source_windows(
    spans: Sequence[tuple[float, float] | None],
    durations: Sequence[float],
    lower: float,
    upper: float,
) -> list[tuple[float, float]]:
    """Source window per sentence inside [lower, upper].

    `spans[i]` is the sentence's own source span, or None for a commentary sentence, which gets the footage
    between its neighbours' moments. Every window is widened to max(MIN_SOURCE_WINDOW_S, 1.5x its audio).
    """
    windows: list[tuple[float, float]] = []
    for position, (span, duration) in enumerate(zip(spans, durations)):
        if span is not None:
            start, end = span
        else:
            previous = next((item for item in reversed(spans[:position]) if item is not None), None)
            following = next((item for item in spans[position + 1 :] if item is not None), None)
            start = previous[1] if previous else lower
            end = following[0] if following else upper
            if end <= start:
                start, end = min(start, end), max(start, end) + 0.001
        start, end = max(lower, min(start, upper)), max(lower, min(end, upper))
        start, end = widen_window(start, max(end, start + 0.001), max(MIN_SOURCE_WINDOW_S, MIN_SOURCE_AUDIO_RATIO * duration), lower, upper)
        windows.append((round(start, 3), round(end, 3)))
    return windows
