from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from common.schema import Shot
from shots.detect import ShotSpan

# Burned-in overlays (site ads, sponsor tickers) stay pixel-identical while the picture under them
# changes, so they show up as edges with the same colour in visually different shots. Subtitles also
# sit on top of the picture but change every few seconds, which the minimum triplet span rules out.
OVERLAY_DETECTOR_VERSION = "static-overlay-v1"
OVERLAY_REASON = "overlay_text"
ANALYSIS_WIDTH = 640
MIN_TRIPLET_SPAN_S = 6.0
MIN_PICTURE_CHANGE = 12.0
COLOR_TOLERANCE = 18
# Measured on a 640px-wide benchmark: ad triplets 0.47-0.51% static edge pixels, every other triplet
# at most 0.08% (p99 0.023%); the threshold sits near their geometric middle.
MIN_STATIC_EDGE_RATIO = 0.002
MIN_EXTENSION_MATCH = 0.5
# A static overlay in this share of all triplets is a persistent watermark, not a time-bounded ad;
# excluding it would remove nearly every shot, so it is only reported.
MAX_FLAGGED_TRIPLET_RATIO = 0.30
EXTEND_LOOKBACK = 4
EXTRACT_CHUNK = 64

SUBTITLE_BAND = (0.86, 0.99)
SUBTITLE_WHITE = 215
SUBTITLE_OUTLINE = 70
MIN_SUBTITLE_TEXT_RATIO = 0.004
HARDSUB_WARNING_RATIO = 0.20


@dataclass
class OverlayFrame:
    span: ShotSpan
    color: np.ndarray
    gray: np.ndarray
    edges: np.ndarray

    @property
    def midpoint(self) -> float:
        return self.span.tc_start + self.span.duration / 2


@dataclass
class OverlayResult:
    overlay_indices: set[int] = field(default_factory=set)
    subtitle_indices: set[int] = field(default_factory=set)
    eligible_triplets: int = 0
    flagged_triplets: int = 0
    max_static_ratio: float = 0.0
    persistent_overlay: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "overlay_indices": sorted(self.overlay_indices),
            "subtitle_indices": sorted(self.subtitle_indices),
            "eligible_triplets": self.eligible_triplets,
            "flagged_triplets": self.flagged_triplets,
            "max_static_ratio": round(self.max_static_ratio, 6),
            "persistent_overlay": self.persistent_overlay,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> "OverlayResult":
        return cls(
            overlay_indices={int(item) for item in payload.get("overlay_indices", [])},  # type: ignore[union-attr]
            subtitle_indices={int(item) for item in payload.get("subtitle_indices", [])},  # type: ignore[union-attr]
            eligible_triplets=int(payload.get("eligible_triplets", 0)),  # type: ignore[arg-type]
            flagged_triplets=int(payload.get("flagged_triplets", 0)),  # type: ignore[arg-type]
            max_static_ratio=float(payload.get("max_static_ratio", 0.0)),  # type: ignore[arg-type]
            persistent_overlay=bool(payload.get("persistent_overlay", False)),
            warnings=[str(item) for item in payload.get("warnings", [])],  # type: ignore[union-attr]
        )


def make_overlay_frame(span: ShotSpan, color: np.ndarray) -> OverlayFrame:
    import cv2

    gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    return OverlayFrame(span=span, color=color.astype(np.int16), gray=gray, edges=cv2.Canny(gray, 80, 160) > 0)


def picture_change(a: OverlayFrame, b: OverlayFrame) -> float:
    return float(np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)).mean())


def static_overlay_mask(a: OverlayFrame, b: OverlayFrame, c: OverlayFrame) -> np.ndarray:
    same_ab = np.abs(a.color - b.color).max(axis=2) < COLOR_TOLERANCE
    same_bc = np.abs(b.color - c.color).max(axis=2) < COLOR_TOLERANCE
    return a.edges & b.edges & c.edges & same_ab & same_bc


def matches_overlay(frame: OverlayFrame, mask: np.ndarray, reference: OverlayFrame) -> bool:
    total = int(mask.sum())
    if total == 0 or frame.edges.shape != mask.shape:
        return False
    same = np.abs(frame.color - reference.color).max(axis=2) < COLOR_TOLERANCE
    return float((frame.edges & mask & same).sum()) / total >= MIN_EXTENSION_MATCH


def has_burned_subtitle(frame: OverlayFrame) -> bool:
    import cv2

    height = frame.gray.shape[0]
    top, bottom = int(height * SUBTITLE_BAND[0]), int(height * SUBTITLE_BAND[1])
    band = frame.color[top:bottom]
    if band.size == 0:
        return False
    white = (band > SUBTITLE_WHITE).all(axis=2)
    outline = cv2.dilate((frame.gray[top:bottom] < SUBTITLE_OUTLINE).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return float((white & outline).mean()) >= MIN_SUBTITLE_TEXT_RATIO


def detect_overlays(frames: Iterable[OverlayFrame]) -> OverlayResult:
    """Mark shots under a time-bounded static overlay and shots showing burned-in subtitles."""
    result = OverlayResult()
    window: deque[OverlayFrame] = deque(maxlen=3)
    history: deque[OverlayFrame] = deque(maxlen=EXTEND_LOOKBACK)
    active: tuple[np.ndarray, OverlayFrame] | None = None
    for frame in frames:
        if has_burned_subtitle(frame):
            result.subtitle_indices.add(frame.span.index)
        if active is not None and frame.span.index not in result.overlay_indices:
            if matches_overlay(frame, *active):
                result.overlay_indices.add(frame.span.index)
            else:
                active = None
        if window and window[-1].gray.shape != frame.gray.shape:
            window.clear()
        if len(window) == 3:
            history.append(window[0])
        window.append(frame)
        if len(window) < 3:
            continue
        a, b, c = window
        if c.midpoint - a.midpoint < MIN_TRIPLET_SPAN_S:
            continue
        if min(picture_change(a, b), picture_change(b, c), picture_change(a, c)) < MIN_PICTURE_CHANGE:
            continue
        result.eligible_triplets += 1
        mask = static_overlay_mask(a, b, c)
        ratio = float(mask.mean())
        result.max_static_ratio = max(result.max_static_ratio, ratio)
        if ratio < MIN_STATIC_EDGE_RATIO:
            continue
        result.flagged_triplets += 1
        result.overlay_indices.update(item.span.index for item in window)
        active = (mask, b)
        for earlier in reversed(history):
            if earlier.span.index in result.overlay_indices or not matches_overlay(earlier, mask, b):
                break
            result.overlay_indices.add(earlier.span.index)
    if result.eligible_triplets and result.flagged_triplets / result.eligible_triplets > MAX_FLAGGED_TRIPLET_RATIO:
        result.persistent_overlay = True
        result.warnings.append(
            f"static overlay found in {result.flagged_triplets}/{result.eligible_triplets} shot triplets; "
            "treated as a persistent watermark, no shots excluded"
        )
        result.overlay_indices.clear()
    return result


def overlay_intervals(spans: list[ShotSpan], indices: set[int]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for span in sorted((span for span in spans if span.index in indices), key=lambda item: item.tc_start):
        if intervals and span.tc_start - intervals[-1][1] <= 1.0:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], span.tc_end))
        else:
            intervals.append((span.tc_start, span.tc_end))
    return intervals


def with_guard_neighbours(spans: list[ShotSpan], indices: set[int]) -> set[int]:
    """Add the shot on each side of an overlay run: the overlay may start or end inside it."""
    ordered = sorted(spans, key=lambda item: (item.tc_start, item.index))
    guarded = set(indices)
    for position, span in enumerate(ordered):
        if span.index not in indices:
            continue
        for neighbour in (position - 1, position + 1):
            if 0 <= neighbour < len(ordered):
                guarded.add(ordered[neighbour].index)
    return guarded


def hardsub_ratio(spans: list[ShotSpan], subtitle_indices: set[int]) -> float:
    total = sum(span.duration for span in spans)
    if total <= 0:
        return 0.0
    return sum(span.duration for span in spans if span.index in subtitle_indices) / total


def _extract_midframe(input_path: Path, span: ShotSpan, width: int, height: int) -> np.ndarray | None:
    midpoint = span.tc_start + span.duration / 2
    completed = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{midpoint:.3f}", "-i", str(input_path),
            "-frames:v", "1", "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ],
        capture_output=True,
        check=False,
    )
    expected = width * height * 3
    if completed.returncode != 0 or len(completed.stdout) < expected:
        return None
    return np.frombuffer(completed.stdout[:expected], np.uint8).reshape(height, width, 3)


def iter_overlay_frames(
    input_path: Path,
    spans: list[ShotSpan],
    *,
    source_width: int,
    source_height: int,
    workers: int = 6,
) -> Iterator[OverlayFrame]:
    """Yield one analysis frame per shot midpoint, in timeline order, decoding in bounded parallel chunks."""
    width = ANALYSIS_WIDTH
    height = max(2, round(source_height * width / max(1, source_width) / 2) * 2)
    ordered = sorted(spans, key=lambda item: (item.tc_start, item.index))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for start in range(0, len(ordered), EXTRACT_CHUNK):
            chunk = ordered[start : start + EXTRACT_CHUNK]
            for span, color in zip(chunk, pool.map(lambda item: _extract_midframe(input_path, item, width, height), chunk)):
                if color is not None:
                    yield make_overlay_frame(span, color)


def apply_overlay_marking(shots: list[Shot], overlay_indices: set[int]) -> list[Shot]:
    output: list[Shot] = []
    for shot in shots:
        if shot.index not in overlay_indices:
            output.append(shot)
            continue
        output.append(
            shot.model_copy(
                update={"is_usable": False, "unusable_reasons": [*shot.unusable_reasons, OVERLAY_REASON]}
            )
        )
    return output
