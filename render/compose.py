from __future__ import annotations

import math
from pathlib import Path

from common.media import run_command
from render.captions import escape_ass_filter_path


def quote_concat_path(path: Path) -> str:
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'"


def concat_list_text(paths: list[Path], durations: list[float] | None = None) -> str:
    if not paths:
        raise ValueError("cannot concat empty temp clip list")
    if durations is not None and len(durations) != len(paths):
        raise ValueError("duration count must match temp clip count")
    lines: list[str] = []
    for index, path in enumerate(paths):
        lines.append(quote_concat_path(path))
        if durations is not None:
            lines.append(f"duration {durations[index]:.6f}")
    return "\n".join(lines) + "\n"

def concat_video(temp_paths: list[Path], output_path: Path, work_dir: Path, durations: list[float] | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    list_file = work_dir / "concat.txt"
    list_file.write_text(concat_list_text(temp_paths, durations), encoding="utf-8")
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

def volume_filter_from_gain(gain_db: float) -> str:
    return f"volume={10 ** (gain_db / 20):.6f}"

def build_bgm_audio_filter(*, audio_duration_s: float, gain_db: float, fade_in_s: float, fade_out_s: float, ducking: str, duck_threshold: float = 0.08, duck_ratio: float = 6.0) -> str:
    fade_in = max(0.0, fade_in_s)
    fade_out = max(0.0, fade_out_s)
    fade_out_start = max(0.0, audio_duration_s - fade_out)
    filters = [
        f"[2:a]atrim=0:{audio_duration_s:.6f},asetpts=PTS-STARTPTS",
        volume_filter_from_gain(gain_db),
    ]
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0 and math.isfinite(fade_out_start):
        filters.append(f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}")
    filters.append("aresample=async=1")
    bgm_chain = ",".join(filters) + "[bgm]"
    if ducking == "sidechain":
        return f"[vo]asplit=2[vo_main][vo_sc];{bgm_chain};[bgm][vo_sc]sidechaincompress=threshold={duck_threshold:.3f}:ratio={duck_ratio:.3f}[bgmduck];[vo_main][bgmduck]amix=inputs=2:duration=first:normalize=0[aout]"
    return f"{bgm_chain};[vo][bgm]amix=inputs=2:duration=first:normalize=0[aout]"

def mux_final(
    *,
    video_path: Path,
    voiceover_path: Path,
    output_path: Path,
    audio_duration_s: float,
    audio_delay_s: float = 0.0,
    bgm_path: Path | None = None,
    bgm_gain_db: float = -20.0,
    bgm_fade_in_s: float = 1.5,
    bgm_fade_out_s: float = 2.5,
    bgm_ducking: str = "none",
    captions_path: Path | None = None,
    crf: int = 20,
    preset: str = "medium",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(voiceover_path)]
    if bgm_path is not None:
        command += ["-stream_loop", "-1", "-i", str(bgm_path)]

    filter_parts: list[str] = []
    if audio_delay_s > 0:
        delay_ms = max(0, round(audio_delay_s * 1000))
        filter_parts.append(f"[1:a]adelay={delay_ms}:all=1,aresample=async=1[vo]")
    else:
        filter_parts.append("[1:a]aresample=async=1[vo]")

    if bgm_path is not None:
        filter_parts.append(build_bgm_audio_filter(
            audio_duration_s=audio_duration_s,
            gain_db=bgm_gain_db,
            fade_in_s=bgm_fade_in_s,
            fade_out_s=bgm_fade_out_s,
            ducking=bgm_ducking,
        ))
        audio_map = "[aout]"
    else:
        audio_map = "[vo]"

    if captions_path is not None:
        filter_parts.append(f"[0:v]ass='{escape_ass_filter_path(captions_path)}'[vout]")
        video_map = "[vout]"
    else:
        video_map = "0:v:0"

    command += ["-filter_complex", ";".join(filter_parts), "-map", video_map, "-map", audio_map]
    if captions_path is not None:
        command += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]
    else:
        command += ["-c:v", "copy"]
    command += ["-c:a", "aac", "-shortest", "-movflags", "+faststart", str(output_path)]
    run_command(command)
