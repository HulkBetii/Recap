from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from common.media import run_command

if TYPE_CHECKING:
    from render.cut import RenderParams


def quote_concat_path(path: Path) -> str:
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'"


def concat_list_text(paths: list[Path]) -> str:
    if not paths:
        raise ValueError("cannot concat empty temp clip list")
    return "\n".join(quote_concat_path(path) for path in paths) + "\n"


def concat_video(temp_paths: list[Path], output_path: Path, work_dir: Path, list_filename: str = "concat.txt") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    list_file = work_dir / list_filename
    list_file.write_text(concat_list_text(temp_paths), encoding="utf-8")
    run_command([
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output_path),
    ])
    return list_file


def tail_pad_frame_count(shortage_s: float, fps: float) -> int:
    if shortage_s <= 0:
        raise ValueError("shortage_s must be greater than zero")
    if fps <= 0:
        raise ValueError("fps must be greater than zero")
    return max(1, math.ceil(shortage_s * fps))


def pad_video_by_tail(*, video_path: Path, output_path: Path, work_dir: Path, shortage_s: float, params: RenderParams) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    pad_frames = tail_pad_frame_count(shortage_s, params.fps)
    last_frame = work_dir / "tail_pad_last_frame.png"
    tail_clip = work_dir / "tail_pad_clip.mp4"
    run_command([
        "ffmpeg",
        "-y",
        "-sseof",
        "-0.1",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-update",
        "1",
        str(last_frame),
    ])
    run_command([
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(last_frame),
        "-an",
        "-vf",
        f"scale={params.width}:{params.height},fps={params.fps:g},format=yuv420p",
        "-frames:v",
        str(pad_frames),
        "-c:v",
        "libx264",
        "-preset",
        params.preset,
        "-crf",
        str(params.crf),
        "-pix_fmt",
        "yuv420p",
        str(tail_clip),
    ])
    concat_video([video_path, tail_clip], output_path, work_dir, list_filename="concat_pad.txt")
    return pad_frames


def pad_video_to_duration(video_path: Path, output_path: Path, duration_s: float) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        "tpad=stop_mode=clone:stop_duration=10",
        "-t",
        f"{duration_s:.6f}",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ])


def mux_voiceover(video_path: Path, voiceover_path: Path, output_path: Path, audio_delay_s: float = 0.0) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(voiceover_path),
        "-map",
        "0:v:0",
    ]
    if audio_delay_s > 0:
        delay_ms = max(0, round(audio_delay_s * 1000))
        command += ["-filter_complex", f"[1:a]adelay={delay_ms}:all=1[a]", "-map", "[a]"]
    else:
        command += ["-map", "1:a:0"]
    command += [
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_command(command)
