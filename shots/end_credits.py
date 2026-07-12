from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from common.schema import Shot
from shots.detect import ShotSpan
from shots.features import to_luma

END_CREDIT_CLASSIFIER_VERSION = "opencv-tail-heuristic-v1"
BLACK_LUMA_THRESHOLD = 20
NON_BLACK_LUMA_THRESHOLD = 25
SATURATION_THRESHOLD = 40
MIN_BLACK_PIXEL_RATIO = 0.90
MAX_SATURATED_PIXEL_RATIO = 0.08
MAX_LARGEST_CONTENT_RATIO = 0.08
MIN_EDGE_DENSITY = 0.001
BLANK_BLACK_PIXEL_RATIO = 0.985


@dataclass(frozen=True)
class CreditFrameMetrics:
    black_pixel_ratio: float
    saturated_pixel_ratio: float
    largest_content_ratio: float
    edge_density: float


def frame_credit_metrics(frame: np.ndarray) -> CreditFrameMetrics:
    import cv2

    gray = to_luma(frame)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    black_pixel_ratio = float(np.mean(gray < BLACK_LUMA_THRESHOLD))
    saturated_pixel_ratio = float(np.mean(hsv[:, :, 1] > SATURATION_THRESHOLD))
    content_mask = (gray > NON_BLACK_LUMA_THRESHOLD).astype(np.uint8)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(content_mask, 8)
    largest_content_ratio = 0.0
    if component_count > 1:
        largest_content_ratio = float(np.max(stats[1:, cv2.CC_STAT_AREA]) / content_mask.size)
    edge_density = float(np.mean(cv2.Canny(gray, 80, 160) > 0))
    return CreditFrameMetrics(
        black_pixel_ratio=black_pixel_ratio,
        saturated_pixel_ratio=saturated_pixel_ratio,
        largest_content_ratio=largest_content_ratio,
        edge_density=edge_density,
    )


def is_credit_only_frame(frame: np.ndarray) -> bool:
    metrics = frame_credit_metrics(frame)
    has_sparse_text_or_blank = (
        metrics.edge_density >= MIN_EDGE_DENSITY
        or metrics.black_pixel_ratio >= BLANK_BLACK_PIXEL_RATIO
    )
    return (
        metrics.black_pixel_ratio >= MIN_BLACK_PIXEL_RATIO
        and metrics.saturated_pixel_ratio <= MAX_SATURATED_PIXEL_RATIO
        and metrics.largest_content_ratio <= MAX_LARGEST_CONTENT_RATIO
        and has_sparse_text_or_blank
    )


def credit_like_score(frames: list[np.ndarray]) -> float:
    if not frames:
        return 0.0
    matched = sum(1 for frame in frames if is_credit_only_frame(frame))
    return round(matched / len(frames), 4)


def tail_shot_spans(spans: list[ShotSpan], *, duration_s: float, tail_s: float) -> list[ShotSpan]:
    tail_start = max(0.0, duration_s - tail_s)
    return [span for span in spans if span.tc_end > tail_start]


def apply_end_credit_marking(
    shots: list[Shot],
    scores_by_index: dict[int, float],
    *,
    threshold: float,
) -> list[Shot]:
    return [
        shot.model_copy(
            update={
                "credit_like_score": round(float(scores_by_index.get(shot.index, 0.0)), 4),
                "is_end_credit": float(scores_by_index.get(shot.index, 0.0)) + 1e-9 >= threshold,
            }
        )
        for shot in shots
    ]
