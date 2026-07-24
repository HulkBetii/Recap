from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise MediaError("ffmpeg was not found on PATH. Install ffmpeg before running media stages.")
    if shutil.which("ffprobe") is None:
        raise MediaError("ffprobe was not found on PATH. Install ffmpeg before running media stages.")


def run_command(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, check=False)
    stderr = _decode_process_output(result.stderr)
    stdout = _decode_process_output(result.stdout)
    if result.returncode != 0:
        message = stderr.strip() or stdout.strip() or "unknown error"
        raise MediaError(message)

def _decode_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def probe_duration(input_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(input_path),
        ],
        capture_output=True,
        check=False,
    )
    stderr = _decode_process_output(result.stderr)
    stdout = _decode_process_output(result.stdout)
    if result.returncode != 0:
        message = stderr.strip() or "ffprobe failed"
        raise MediaError(message)
    try:
        payload: dict[str, Any] = json.loads(stdout)
        return float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("could not read media duration with ffprobe") from exc

def _parse_rate(rate: str) -> float:
    if "/" not in rate:
        return float(rate)
    numerator, denominator = rate.split("/", 1)
    denominator_value = float(denominator)
    if denominator_value == 0:
        raise ValueError("rate denominator cannot be zero")
    return float(numerator) / denominator_value

def probe_video_stream(input_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name,r_frame_rate,avg_frame_rate,duration",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(input_path),
        ],
        capture_output=True,
        check=False,
    )
    stderr = _decode_process_output(result.stderr)
    stdout = _decode_process_output(result.stdout)
    if result.returncode != 0:
        message = stderr.strip() or "ffprobe failed"
        raise MediaError(message)
    try:
        payload: dict[str, Any] = json.loads(stdout)
        stream = payload["streams"][0]
        rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
        fps = _parse_rate(rate_text)
        duration = stream.get("duration") or payload.get("format", {}).get("duration")
        return {
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "codec": str(stream.get("codec_name") or "unknown"),
            "fps": fps,
            "duration": float(duration),
        }
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("could not read video stream with ffprobe") from exc

def has_audio_stream(input_path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(input_path),
        ],
        capture_output=True,
        check=False,
    )
    stderr = _decode_process_output(result.stderr)
    stdout = _decode_process_output(result.stdout)
    if result.returncode != 0:
        message = stderr.strip() or "ffprobe failed"
        raise MediaError(message)
    try:
        payload: dict[str, Any] = json.loads(stdout)
        return bool(payload.get("streams"))
    except json.JSONDecodeError as exc:
        raise MediaError("could not read audio streams with ffprobe") from exc


def extract_audio(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_path),
    ])


def extract_frame(input_path: Path, timestamp: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ])


def normalize_audio(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-af",
        "loudnorm=I=-14:TP=-1:LRA=11",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ])


def generate_silence(output_path: Path, duration_s: float) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=44100:cl=mono",
        "-t",
        f"{duration_s:.3f}",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ])


def concat_audio(inputs: list[Path], output_path: Path) -> None:
    if not inputs:
        raise MediaError("cannot concat empty audio input list")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.with_suffix(".concat.txt")
    list_text = "\n".join(f"file '{path.resolve().as_posix()}'" for path in inputs)
    list_file.write_text(list_text + "\n", encoding="utf-8")
    try:
        run_command([
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output_path),
        ])
    finally:
        try:
            list_file.unlink()
        except FileNotFoundError:
            pass
