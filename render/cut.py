from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from common.media import run_command
from common.schema import EdlPlacement
from render.cache import file_identity, stable_hash
from render.quantize import FramePlacement

@dataclass(frozen=True)
class RenderParams:
    width: int
    height: int
    fps: float
    fit: str
    crf: int
    preset: str

@dataclass(frozen=True)
class ClampedSource:
    src_in: float
    src_out: float
    warnings: tuple[str, ...]


def clamp_source(placement: EdlPlacement, film_duration: float) -> ClampedSource:
    warnings: list[str] = []
    src_in = max(0.0, min(placement.src_in, film_duration))
    src_out = max(0.0, min(placement.src_out, film_duration))
    if src_in != placement.src_in or src_out != placement.src_out:
        warnings.append(f"placement #{placement.beat_id}/{placement.shot_index} source span was clamped")
    if src_out <= src_in:
        src_out = min(film_duration, src_in + 0.001)
        if src_out <= src_in:
            src_in = max(0.0, film_duration - 0.001)
            src_out = film_duration
        warnings.append(f"placement #{placement.beat_id}/{placement.shot_index} source span is too short after clamp")
    return ClampedSource(src_in=src_in, src_out=src_out, warnings=tuple(warnings))


def build_video_filter(*, params: RenderParams, frame_count: int, source_duration: float, target_duration: float, speed: float) -> str:
    if params.fit != "cover":
        raise ValueError("only fit=cover is supported in v1")
    scale_crop = (
        f"scale={params.width}:{params.height}:force_original_aspect_ratio=increase,"
        f"crop={params.width}:{params.height}"
    )
    filters = [scale_crop, f"fps={params.fps:g}"]
    if abs(speed - 1.0) > 1e-6:
        filters.append(f"setpts=PTS/{speed:.6f}")
    elif abs(source_duration - target_duration) > 1e-3 and source_duration > 0:
        ratio = source_duration / target_duration
        filters.append(f"setpts=PTS/{ratio:.6f}")
    filters.extend([f"trim=duration={target_duration:.6f}", "setpts=PTS-STARTPTS", "format=yuv420p"])
    return ",".join(filters)


def temp_cache_key(*, film_path: Path, frame: FramePlacement, source: ClampedSource, params: RenderParams) -> str:
    placement = frame.placement
    return stable_hash({
        "film": file_identity(film_path),
        "src_in": round(source.src_in, 6),
        "src_out": round(source.src_out, 6),
        "speed": round(placement.speed, 6),
        "frame_count": frame.frame_count,
        "width": params.width,
        "height": params.height,
        "fps": params.fps,
        "fit": params.fit,
        "crf": params.crf,
        "preset": params.preset,
    })


def cut_temp_clip(*, film_path: Path, output_path: Path, frame: FramePlacement, source: ClampedSource, params: RenderParams) -> None:
    source_duration = source.src_out - source.src_in
    filter_text = build_video_filter(
        params=params,
        frame_count=frame.frame_count,
        source_duration=source_duration,
        target_duration=frame.duration_s,
        speed=frame.placement.speed,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-ss",
        f"{source.src_in:.6f}",
        "-i",
        str(film_path),
        "-t",
        f"{source_duration:.6f}",
        "-an",
        "-vf",
        filter_text,
        "-frames:v",
        str(frame.frame_count),
        "-c:v",
        "libx264",
        "-preset",
        params.preset,
        "-crf",
        str(params.crf),
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ])
