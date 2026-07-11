from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import subprocess

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
    scene_threshold: float = 0.3,
    scene_scale_width: int = 640,
    scene_min_gap: float = 0.3,
    max_shot_len: float = 0.0,
) -> tuple[list[ShotSpan], float]:
    duration = probe_duration(input_path)
    start_bound = max(0.0, skip_intro)
    end_bound = max(start_bound, duration - max(0.0, skip_outro))
    if detector == "ffmpeg-scene":
        raw_scenes = run_ffmpeg_scene(
            input_path,
            duration=duration,
            threshold=scene_threshold,
            scale_width=scene_scale_width,
            min_gap=scene_min_gap,
        )
    else:
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
    clipped = split_long_scenes(clipped, max_shot_len=max_shot_len)
    spans: list[ShotSpan] = []
    for start, end in clipped:
        rounded_start = max(0.0, round(start, 3))
        rounded_end = min(duration, round(end, 3))
        if rounded_end > rounded_start:
            spans.append(ShotSpan(index=len(spans), tc_start=rounded_start, tc_end=rounded_end))
    return spans, duration

def split_long_scenes(scenes: list[tuple[float, float]], *, max_shot_len: float) -> list[tuple[float, float]]:
    if max_shot_len <= 0:
        return scenes
    split: list[tuple[float, float]] = []
    for start, end in scenes:
        duration = end - start
        if duration <= max_shot_len:
            split.append((start, end))
            continue
        n_parts = max(1, math.ceil(duration / max_shot_len))
        step = duration / n_parts
        for index in range(n_parts):
            part_start = start + (step * index)
            part_end = end if index == n_parts - 1 else start + (step * (index + 1))
            if part_end > part_start:
                split.append((part_start, part_end))
    return split

def run_pyscenedetect(input_path: Path, *, detector: str, downscale: str) -> list[tuple[float, float]]:
    try:
        return run_pyscenedetect_open_video(input_path, detector=detector, downscale=downscale)
    except (ImportError, AttributeError):
        return run_pyscenedetect_video_manager(input_path, detector=detector, downscale=downscale)

_SHOWINFO_PTS_RE = re.compile(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)")

def run_ffmpeg_scene(input_path: Path, *, duration: float, threshold: float, scale_width: int, min_gap: float) -> list[tuple[float, float]]:
    if not 0.0 < threshold < 1.0:
        raise ValueError("--scene-threshold must be between 0 and 1")
    if scale_width < 0:
        raise ValueError("--scene-scale-width must be >= 0")
    if min_gap < 0:
        raise ValueError("--scene-min-gap must be >= 0")
    filters: list[str] = []
    if scale_width > 0:
        filters.append(f"scale={scale_width}:-2")
    filters.append(f"select='gt(scene,{threshold:.4f})'")
    filters.append("showinfo")
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-threads",
            "0",
            "-i",
            str(input_path),
            "-vf",
            ",".join(filters),
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "ffmpeg scene detection failed"
        raise RuntimeError(message.splitlines()[-1])
    boundaries: list[float] = []
    for line in result.stderr.splitlines() + result.stdout.splitlines():
        match = _SHOWINFO_PTS_RE.search(line)
        if match:
            boundaries.append(float(match.group(1)))
    return boundaries_to_scenes(boundaries, duration=duration, min_gap=min_gap)

def boundaries_to_scenes(boundaries: list[float], *, duration: float, min_gap: float) -> list[tuple[float, float]]:
    points = [0.0]
    last = 0.0
    for boundary in sorted(set(round(value, 3) for value in boundaries)):
        if boundary <= 0.0 or boundary >= duration:
            continue
        if boundary - last < min_gap:
            continue
        points.append(boundary)
        last = boundary
    if duration > points[-1]:
        points.append(duration)
    return [(start, end) for start, end in zip(points, points[1:]) if end > start]

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
