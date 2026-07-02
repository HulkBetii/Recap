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
    try:
        return run_pyscenedetect_open_video(input_path, detector=detector, downscale=downscale)
    except (ImportError, AttributeError):
        return run_pyscenedetect_video_manager(input_path, detector=detector, downscale=downscale)

def build_detector(detector: str):  # type: ignore[no-untyped-def]
    from scenedetect import AdaptiveDetector, ContentDetector

    if detector == "content":
        return ContentDetector()
    if detector == "adaptive":
        return AdaptiveDetector()
    raise ValueError(f"Unsupported detector: {detector}")

def run_pyscenedetect_open_video(input_path: Path, *, detector: str, downscale: str) -> list[tuple[float, float]]:
    from scenedetect import SceneManager, open_video

    scene_manager = SceneManager()
    scene_manager.add_detector(build_detector(detector))
    video = open_video(str(input_path))
    if downscale != "auto" and hasattr(video, "set_downscale_factor"):
        video.set_downscale_factor(int(downscale))
    scene_manager.detect_scenes(video=video)
    scenes = scene_manager.get_scene_list()
    return [(start.get_seconds(), end.get_seconds()) for start, end in scenes]

def run_pyscenedetect_video_manager(input_path: Path, *, detector: str, downscale: str) -> list[tuple[float, float]]:
    from scenedetect import SceneManager
    try:
        from scenedetect import VideoManager
    except ImportError:
        from scenedetect.video_manager import VideoManager

    video_manager = VideoManager([str(input_path)])
    scene_manager = SceneManager()
    scene_manager.add_detector(build_detector(detector))
    try:
        if downscale != "auto":
            video_manager.set_downscale_factor(int(downscale))
        video_manager.start()
        scene_manager.detect_scenes(frame_source=video_manager)
        scenes = scene_manager.get_scene_list()
        return [(start.get_seconds(), end.get_seconds()) for start, end in scenes]
    finally:
        video_manager.release()
