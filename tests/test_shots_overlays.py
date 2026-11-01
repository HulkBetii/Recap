from __future__ import annotations

import cv2
import numpy as np

from common.schema import Shot
from shots.detect import ShotSpan
from shots.overlays import (
    OVERLAY_REASON,
    OverlayResult,
    apply_overlay_marking,
    detect_overlays,
    has_burned_subtitle,
    hardsub_ratio,
    make_overlay_frame,
    overlay_intervals,
    with_guard_neighbours,
)

WIDTH, HEIGHT = 640, 268


def scene(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, size=(HEIGHT // 8, WIDTH // 8, 3), dtype=np.uint8)
    return cv2.resize(image, (WIDTH, HEIGHT), interpolation=cv2.INTER_CUBIC)


def draw_text(image: np.ndarray, text: str, origin: tuple[int, int]) -> np.ndarray:
    output = image.copy()
    cv2.putText(output, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 6, cv2.LINE_AA)
    cv2.putText(output, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    return output


def spans(count: int) -> list[ShotSpan]:
    return [ShotSpan(index=index, tc_start=index * 4.0, tc_end=index * 4.0 + 4.0) for index in range(count)]


def frames_for(images: list[np.ndarray]) -> list:
    return [make_overlay_frame(span, image) for span, image in zip(spans(len(images)), images)]


def test_time_bounded_overlay_marks_only_the_covered_shots() -> None:
    images = [scene(seed) for seed in range(10)]
    for index in range(3, 7):
        images[index] = draw_text(draw_text(images[index], "SPONSOR AD 24/7 BET NOW", (40, 40)), "WIN BIG TODAY", (40, 80))

    result = detect_overlays(frames_for(images))

    assert result.overlay_indices == {3, 4, 5, 6}
    assert result.persistent_overlay is False
    assert overlay_intervals(spans(10), result.overlay_indices) == [(12.0, 28.0)]


def test_persistent_watermark_is_reported_but_not_excluded() -> None:
    images = [draw_text(scene(seed), "SITE LOGO WATERMARK", (40, 40)) for seed in range(8)]

    result = detect_overlays(frames_for(images))

    assert result.persistent_overlay is True
    assert result.overlay_indices == set()
    assert "persistent watermark" in result.warnings[0]


def test_clean_scenes_have_no_overlay() -> None:
    result = detect_overlays(frames_for([scene(seed) for seed in range(8)]))

    assert result.overlay_indices == set()
    assert result.eligible_triplets == 6
    assert result.flagged_triplets == 0


def test_burned_subtitle_detection_and_ratio() -> None:
    plain = make_overlay_frame(spans(1)[0], np.full((HEIGHT, WIDTH, 3), 90, dtype=np.uint8))
    subbed_image = draw_text(np.full((HEIGHT, WIDTH, 3), 90, dtype=np.uint8), "Toi khong biet anh ta la ai", (120, 256))
    subbed = make_overlay_frame(spans(1)[0], subbed_image)

    assert has_burned_subtitle(subbed) is True
    assert has_burned_subtitle(plain) is False
    assert hardsub_ratio(spans(4), {0, 1}) == 0.5


def test_guard_neighbours_and_marking() -> None:
    assert with_guard_neighbours(spans(6), {2, 3}) == {1, 2, 3, 4}
    shot = Shot(
        src="film.mp4",
        index=1,
        tc_start=4.0,
        tc_end=8.0,
        duration=4.0,
        thumb="shots/film-001.jpg",
        motion_score=0.2,
        face_count=1,
        face_area=0.05,
        brightness=0.4,
        is_usable=True,
    )
    marked = apply_overlay_marking([shot], {1})[0]
    assert marked.is_usable is False
    assert OVERLAY_REASON in marked.unusable_reasons


def test_overlay_result_round_trips_through_cache_json() -> None:
    result = OverlayResult(overlay_indices={3, 4}, subtitle_indices={1}, eligible_triplets=5, flagged_triplets=1)
    assert OverlayResult.from_json(result.to_json()).to_json() == result.to_json()
