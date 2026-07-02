from __future__ import annotations

import logging
from pathlib import Path

from common.media import extract_frame
from common.schema import SilentGap, VisionSegment
from ingest.llm import VISION_UNAVAILABLE, OpenAIIngestClient


def describe_gaps(
    *,
    input_path: Path,
    gaps: list[SilentGap],
    frames_dir: Path,
    client: OpenAIIngestClient,
    logger: logging.Logger,
) -> tuple[list[VisionSegment], int]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    results: list[VisionSegment] = []
    warnings_count = 0
    for gap in gaps:
        frame_path = frames_dir / f"gap-{gap.id:04d}.jpg"
        if not frame_path.exists():
            extract_frame(input_path, gap.midpoint, frame_path)
        try:
            scene_desc = client.describe_frame(frame_path)
        except Exception as exc:  # noqa: BLE001
            warnings_count += 1
            logger.warning("vision failed for gap #%s: %s", gap.id, exc)
            scene_desc = VISION_UNAVAILABLE
        results.append(
            VisionSegment(
                gap_id=gap.id,
                tc_start=gap.tc_start,
                tc_end=gap.tc_end,
                scene_desc=scene_desc,
            )
        )
    return results, warnings_count
