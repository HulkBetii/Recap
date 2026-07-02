from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from common.media import probe_duration


@dataclass(frozen=True)
class ShotSpan:
    index: int
    tc_start: float
    tc_end: float

    @property
    def duration(self) -> float:
        return self.tc_end - self.tc_start


def detect_shots(
    input_path: Path,
    *,
    detector: str,
    skip_intro: float,
    skip_outro: float,
    downscale: str,
) -> tuple[list[ShotSpan], float]:
    duration = probe_duration(input_path)
    start_bound = max(0.0, skip_intro)
    end_bound = max(start_bound, duration - max(0.0, skip_outro))
    raw_scenes = run_pyscenedetect(input_path, detector=detector, downscale=downscale)
    if not raw_scenes:
        raw_scenes = [(0.0, duration)]
    clipped: list[tuple[float, float]] = []
    for start, end in raw_scenes:
        clipped_start = max(start_bound, start)
        clipped_end = min(end_bound, end)
        if clipped_end > clipped_start:
            clipped.append((clipped_start, clipped_end))
    if not clipped:
        clipped = [(start_bound, end_bound)]
    return [ShotSpan(index=index, tc_start=round(start, 3), tc_end=round(end, 3)) for index, (start, end) in enumerate(clipped)], duration


def run_pyscenedetect(input_path: Path, *, detector: str, downscale: str) -> list[tuple[float, float]]:
    from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, VideoManager

    video_manager = VideoManager([str(input_path)])
    scene_manager = SceneManager()
    if detector == "content":
        scene_manager.add_detector(ContentDetector())
    elif detector == "adaptive":
        scene_manager.add_detector(AdaptiveDetector())
    else:
        raise ValueError(f"Unsupported detector: {detector}")
    try:
        if downscale != "auto":
            video_manager.set_downscale_factor(int(downscale))
        video_manager.start()
        scene_manager.detect_scenes(frame_source=video_manager)
        scenes = scene_manager.get_scene_list()
        return [(start.get_seconds(), end.get_seconds()) for start, end in scenes]
    finally:
        video_manager.release()
