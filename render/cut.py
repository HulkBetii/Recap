from __future__ import annotations

import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from common.media import run_command
from common.schema import EdlPlacement, PlacementEdit
from render.cache import file_identity, stable_hash
from render.quantize import FramePlacement

ENCODERS = ("auto", "libx264", "h264_nvenc")
NVENC_PRESET = "p5"


@dataclass(frozen=True)
class RenderParams:
    width: int
    height: int
    fps: float
    fit: str
    crf: int
    preset: str
    encoder: str = "libx264"
    nvenc_cq: int = 24


def format_fps(fps: float) -> str:
    """Return an exact ffmpeg rate: whole rates stay '30', NTSC rates become '24000/1001'."""
    rate = Fraction(fps).limit_denominator(1001)
    if rate.denominator == 1:
        return str(rate.numerator)
    return f"{rate.numerator}/{rate.denominator}"


def video_encoder_args(params: RenderParams) -> list[str]:
    """Encoder flags shared by temp clips and the tail pad so concat -c copy sees one stream format."""
    if params.encoder == "h264_nvenc":
        # p5/cq24 matched libx264 medium/crf20 SSIM at a similar bitrate and ran ~3x faster in R7 tests.
        return [
            "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-tune", "hq",
            "-rc", "vbr", "-cq", str(params.nvenc_cq), "-b:v", "0", "-bf", "3",
        ]
    return ["-c:v", "libx264", "-preset", params.preset, "-crf", str(params.crf)]


def nvenc_available() -> bool:
    """Probe a one-frame NVENC encode; listing the encoder is not enough without a usable GPU/driver."""
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30:d=0.1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_encoder(requested: str) -> str:
    if requested not in ENCODERS:
        raise ValueError(f"unsupported encoder: {requested}")
    if requested != "auto":
        return requested
    return "h264_nvenc" if nvenc_available() else "libx264"

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
    filters = [scale_crop]
    if abs(speed - 1.0) > 1e-6:
        filters.append(f"setpts=PTS/{speed:.6f}")
    elif abs(source_duration - target_duration) > 1e-3 and source_duration > 0:
        ratio = source_duration / target_duration
        filters.append(f"setpts=PTS/{ratio:.6f}")
    filters.extend([
        f"fps={format_fps(params.fps)}",
        f"tpad=stop_mode=clone:stop={frame_count}",
        f"trim=end_frame={frame_count}",
        "setpts=PTS-STARTPTS",
        "format=yuv420p",
    ])
    return ",".join(filters)


def _speed_slices(*, frame_count: int, fps: float, edit: PlacementEdit, fallback_speed: float) -> list[tuple[int, float]]:
    freeze_frames = min(max(0, round(edit.freeze_duration_s * fps)), max(0, frame_count - 1))
    moving_frames = frame_count - freeze_frames
    if not edit.speed_ramp:
        return [(moving_frames, fallback_speed)]
    slices: list[tuple[int, float]] = []
    previous_frame = 0
    for segment in edit.speed_ramp:
        end_frame = round(segment.end_ratio * moving_frames)
        segment_frames = end_frame - previous_frame
        if segment_frames > 0:
            if slices and abs(slices[-1][1] - segment.speed) <= 1e-6:
                slices[-1] = (slices[-1][0] + segment_frames, segment.speed)
            else:
                slices.append((segment_frames, segment.speed))
        previous_frame = end_frame
    if previous_frame != moving_frames:
        raise ValueError("speed ramp does not cover the moving placement frames")
    return slices


def required_enhanced_source_duration(
    *, frame_count: int, fps: float, edit: PlacementEdit, fallback_speed: float
) -> float:
    return sum(
        output_frames / fps * speed
        for output_frames, speed in _speed_slices(
            frame_count=frame_count,
            fps=fps,
            edit=edit,
            fallback_speed=fallback_speed,
        )
    )


def build_enhanced_video_filter(
    *,
    params: RenderParams,
    frame_count: int,
    fallback_speed: float,
    edit: PlacementEdit,
) -> str:
    if params.fit != "cover":
        raise ValueError("only fit=cover is supported in v1")
    freeze_frames = min(max(0, round(edit.freeze_duration_s * params.fps)), max(0, frame_count - 1))
    slices = _speed_slices(frame_count=frame_count, fps=params.fps, edit=edit, fallback_speed=fallback_speed)
    chains = [
        f"[0:v]setpts=PTS-STARTPTS,scale={params.width}:{params.height}:force_original_aspect_ratio=increase,"
        f"crop={params.width}:{params.height}[base]"
    ]
    if len(slices) > 1:
        split_labels = "".join(f"[slice{index}]" for index in range(len(slices)))
        chains.append(f"[base]split={len(slices)}{split_labels}")
    source_cursor = 0.0
    output_labels: list[str] = []
    for index, (output_frames, speed) in enumerate(slices):
        source_duration = output_frames / params.fps * speed
        input_label = "base" if len(slices) == 1 else f"slice{index}"
        output_label = f"timed{index}"
        chains.append(
            f"[{input_label}]trim=start={source_cursor:.6f}:end={source_cursor + source_duration:.6f},"
            f"setpts=(PTS-STARTPTS)/{speed:.6f},fps={format_fps(params.fps)},"
            f"tpad=stop_mode=clone:stop={output_frames},trim=end_frame={output_frames},"
            f"setpts=PTS-STARTPTS[{output_label}]"
        )
        output_labels.append(f"[{output_label}]")
        source_cursor += source_duration
    if len(output_labels) == 1:
        timed_label = "timed0"
    else:
        chains.append(f"{''.join(output_labels)}concat=n={len(output_labels)}:v=1:a=0[timed]")
        timed_label = "timed"
    grade = f"eq=contrast={edit.contrast:.6f}:saturation={edit.saturation:.6f}:brightness={edit.brightness:.6f}"
    if abs(edit.warmth) > 1e-6:
        grade += f",colorbalance=rs={edit.warmth:.6f}:bs={-edit.warmth:.6f}"
    visual_filters = [
        f"tpad=stop_mode=clone:stop={freeze_frames},trim=end_frame={frame_count}",
        grade,
    ]
    if (
        abs(edit.zoom_start - 1.0) > 1e-6
        or abs(edit.zoom_end - 1.0) > 1e-6
        or abs(edit.pan_x) > 1e-6
        or abs(edit.pan_y) > 1e-6
    ):
        denominator = max(1, frame_count - 1)
        zoom_expression = f"{edit.zoom_start:.6f}+({edit.zoom_end - edit.zoom_start:.6f})*on/{denominator}"
        x_expression = f"max(0,min(iw-iw/zoom,iw/2-iw/zoom/2+({edit.pan_x:.6f})*iw))"
        y_expression = f"max(0,min(ih-ih/zoom,ih/2-ih/zoom/2+({edit.pan_y:.6f})*ih))"
        visual_filters.append(
            f"zoompan=z='{zoom_expression}':x='{x_expression}':y='{y_expression}':"
            f"d=1:s={params.width}x{params.height}:fps={format_fps(params.fps)}"
        )
    if edit.aspect_ratio is not None:
        content_height = min(params.height, round(params.width / edit.aspect_ratio))
        bar_height = max(0, (params.height - content_height) // 2)
        if bar_height > 0:
            visual_filters.extend([
                f"drawbox=x=0:y=0:w=iw:h={bar_height}:color=black:t=fill",
                f"drawbox=x=0:y=ih-{bar_height}:w=iw:h={bar_height}:color=black:t=fill",
            ])
    visual_filters.extend([f"trim=end_frame={frame_count}", "setpts=PTS-STARTPTS", "setsar=1", "format=yuv420p"])
    chains.append(f"[{timed_label}]{','.join(visual_filters)}[vout]")
    return ";".join(chains)


def temp_cache_key(
    *,
    film_path: Path,
    frame: FramePlacement,
    source: ClampedSource,
    params: RenderParams,
    edit: PlacementEdit | None = None,
) -> str:
    placement = frame.placement
    payload = {
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
        "edit": edit.model_dump(mode="json") if edit is not None else None,
    }
    # libx264 keeps its historical key so existing temp clip caches stay valid.
    if params.encoder != "libx264":
        payload["encoder"] = params.encoder
        payload["nvenc_cq"] = params.nvenc_cq
    return stable_hash(payload)


def cut_temp_clip(
    *,
    film_path: Path,
    output_path: Path,
    frame: FramePlacement,
    source: ClampedSource,
    params: RenderParams,
    edit: PlacementEdit | None = None,
) -> None:
    source_duration = source.src_out - source.src_in
    if edit is None or edit.disabled:
        filter_text = build_video_filter(
            params=params,
            frame_count=frame.frame_count,
            source_duration=source_duration,
            target_duration=frame.duration_s,
            speed=frame.placement.speed,
        )
        filter_args = ["-vf", filter_text]
    else:
        filter_text = build_enhanced_video_filter(
            params=params,
            frame_count=frame.frame_count,
            fallback_speed=frame.placement.speed,
            edit=edit,
        )
        filter_args = ["-filter_complex", filter_text, "-map", "[vout]"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f".{output_path.stem}.{uuid4().hex}.partial{output_path.suffix}")
    try:
        run_command([
            "ffmpeg",
            "-y",
            "-ss",
            f"{source.src_in:.6f}",
            "-t",
            f"{source_duration:.6f}",
            "-i",
            str(film_path),
            "-an",
            *filter_args,
            "-frames:v",
            str(frame.frame_count),
            *video_encoder_args(params),
            "-pix_fmt",
            "yuv420p",
            str(partial_path),
        ])
        partial_path.replace(output_path)
    finally:
        partial_path.unlink(missing_ok=True)
