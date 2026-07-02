from __future__ import annotations

from pathlib import Path

from common.media import run_command


def quote_concat_path(path: Path) -> str:
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'"


def concat_list_text(paths: list[Path]) -> str:
    if not paths:
        raise ValueError("cannot concat empty temp clip list")
    return "\n".join(quote_concat_path(path) for path in paths) + "\n"


def concat_video(temp_paths: list[Path], output_path: Path, work_dir: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    list_file = work_dir / "concat.txt"
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


def mux_voiceover(video_path: Path, voiceover_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(voiceover_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ])
