from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from common.media import MediaError, probe_duration, run_command

ENCODE_TRUE_PEAK_HEADROOM_DB = 0.3
COMMENTARY_AUDIO_NORMALIZATION_VERSION = "reaction-commentary-audio-v1"


def normalization_cache_signature() -> dict[str, str | float]:
    return {
        "version": COMMENTARY_AUDIO_NORMALIZATION_VERSION,
        "encode_true_peak_headroom_db": ENCODE_TRUE_PEAK_HEADROOM_DB,
    }


@dataclass(frozen=True)
class AudioMetrics:
    duration_s: float
    lufs_i: float | None
    true_peak_dbfs: float | None


def normalize_commentary_audio(
    input_path: Path,
    output_path: Path,
    *,
    trim_handle_ms: int = 80,
    target_lufs: float = -14.0,
    max_true_peak_db: float = -2.0,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle_s = max(0.0, trim_handle_ms / 1000.0)
    normalization_peak = max_true_peak_db - ENCODE_TRUE_PEAK_HEADROOM_DB
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-af",
            (
                "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB:"
                f"start_silence={handle_s:.3f},areverse,"
                "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB:"
                f"start_silence={handle_s:.3f},areverse,"
                f"loudnorm=I={target_lufs:g}:TP={normalization_peak:g}:LRA=7"
            ),
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output_path),
        ]
    )


def pad_audio_tail(input_path: Path, output_path: Path, pad_s: float) -> None:
    if pad_s <= 0:
        raise ValueError("pad_s must be positive")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-af",
            f"apad=pad_dur={pad_s:.6f}",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output_path),
        ]
    )


def measure_audio(path: Path) -> AudioMetrics:
    duration = probe_duration(path)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-14:TP=-2:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or "ffmpeg loudness measurement failed")
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.DOTALL)
    if not matches:
        return AudioMetrics(duration_s=duration, lufs_i=None, true_peak_dbfs=None)
    try:
        payload = json.loads(matches[-1])
        return AudioMetrics(
            duration_s=duration,
            lufs_i=float(payload["input_i"]),
            true_peak_dbfs=float(payload["input_tp"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return AudioMetrics(duration_s=duration, lufs_i=None, true_peak_dbfs=None)
