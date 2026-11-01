from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from common.media import extract_frame
from common.schema import SilentGap, VisionSegment
from ingest.llm import VISION_UNAVAILABLE, VisionUnavailableError


class VisionClient(Protocol):
    def describe_frame(self, frame_path: Path) -> str: ...


class VisionCircuitOpen(RuntimeError):
    """Vision stopped early on a permanent error; carries the partial results for this run only."""

    def __init__(self, message: str, *, segments: list[VisionSegment], warnings_count: int) -> None:
        super().__init__(message)
        self.segments = segments
        self.warnings_count = warnings_count


def describe_gaps(
    *,
    input_path: Path,
    gaps: list[SilentGap],
    frames_dir: Path,
    client: VisionClient,
    logger: logging.Logger,
) -> tuple[list[VisionSegment], int]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_paths: list[Path] = []
    for gap in gaps:
        frame_path = frames_dir / f"gap-{gap.id:04d}.jpg"
        if not frame_path.exists():
            extract_frame(input_path, gap.midpoint, frame_path)
        frame_paths.append(frame_path)

    describe_many = getattr(client, "describe_frames", None)
    outcomes: list[str | Exception]
    stop_error: VisionUnavailableError | None = None
    if callable(describe_many):
        outcomes = describe_many(frame_paths)
        stop_error = next((item for item in outcomes if isinstance(item, VisionUnavailableError)), None)
    else:
        outcomes = []
        for frame_path in frame_paths:
            if stop_error is not None:
                outcomes.append(stop_error)
                continue
            try:
                outcomes.append(client.describe_frame(frame_path))
            except VisionUnavailableError as exc:
                stop_error = exc
                outcomes.append(exc)
            except Exception as exc:  # noqa: BLE001 - one frame failing must not stop the others
                outcomes.append(exc)

    results: list[VisionSegment] = []
    warnings_count = 0
    for gap, outcome in zip(gaps, outcomes):
        if isinstance(outcome, Exception):
            warnings_count += 1
            if not isinstance(outcome, VisionUnavailableError):
                logger.warning("vision failed for gap #%s: %s", gap.id, outcome)
            scene_desc = VISION_UNAVAILABLE
        else:
            scene_desc = outcome
        results.append(VisionSegment(gap_id=gap.id, tc_start=gap.tc_start, tc_end=gap.tc_end, scene_desc=scene_desc))
    if stop_error is not None:
        skipped = sum(isinstance(outcome, VisionUnavailableError) for outcome in outcomes)
        logger.error("vision stopped after a permanent error; %d frame(s) left undescribed: %s", skipped, stop_error)
        raise VisionCircuitOpen(str(stop_error), segments=results, warnings_count=warnings_count)
    return results, warnings_count
