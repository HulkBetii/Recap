from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SilenceDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimeSpan:
    tc_start: float
    tc_end: float

    @property
    def duration_s(self) -> float:
        return self.tc_end - self.tc_start


_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silences(
    audio_path: Path,
    *,
    noise_db: float,
    min_silence_s: float,
    duration_s: float,
) -> list[TimeSpan]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio_path),
            "-af",
            f"silencedetect=n={noise_db}dB:d={min_silence_s}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise SilenceDetectionError(result.stderr.strip() or "ffmpeg silencedetect failed")
    spans: list[TimeSpan] = []
    pending_start: float | None = None
    for line in result.stderr.splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            pending_start = max(0.0, float(start_match.group(1)))
        end_match = _SILENCE_END.search(line)
        if end_match and pending_start is not None:
            end = min(duration_s, float(end_match.group(1)))
            if end > pending_start:
                spans.append(TimeSpan(pending_start, end))
            pending_start = None
    if pending_start is not None and pending_start < duration_s:
        spans.append(TimeSpan(pending_start, duration_s))
    return spans


def speech_spans(duration_s: float, silences: list[TimeSpan], *, min_speech_s: float = 0.15) -> list[TimeSpan]:
    output: list[TimeSpan] = []
    cursor = 0.0
    for silence in sorted(silences, key=lambda item: item.tc_start):
        start = max(cursor, silence.tc_start)
        if start - cursor >= min_speech_s:
            output.append(TimeSpan(cursor, start))
        cursor = max(cursor, silence.tc_end)
    if duration_s - cursor >= min_speech_s:
        output.append(TimeSpan(cursor, duration_s))
    return output


def split_analysis_regions(
    spans: list[TimeSpan],
    *,
    max_region_s: float,
    overlap_s: float,
) -> list[TimeSpan]:
    if max_region_s <= 0:
        raise ValueError("max_region_s must be positive")
    if overlap_s < 0 or overlap_s >= max_region_s:
        raise ValueError("overlap_s must satisfy 0 <= overlap_s < max_region_s")
    output: list[TimeSpan] = []
    for span in spans:
        cursor = span.tc_start
        while cursor < span.tc_end - 1e-6:
            end = min(span.tc_end, cursor + max_region_s)
            output.append(TimeSpan(cursor, end))
            if end >= span.tc_end - 1e-6:
                break
            cursor = end - overlap_s
    return output


def split_at_silence_midpoints(
    duration_s: float,
    silences: list[TimeSpan],
    *,
    max_region_s: float,
    overlap_s: float,
) -> list[TimeSpan]:
    """Build full-timeline ASR regions, preferring silence midpoints as boundaries."""
    boundaries = [0.0]
    for silence in sorted(silences, key=lambda item: (item.tc_start, item.tc_end)):
        start = max(0.0, min(duration_s, silence.tc_start))
        end = max(start, min(duration_s, silence.tc_end))
        midpoint = start + (end - start) / 2
        if boundaries[-1] + 1e-6 < midpoint < duration_s - 1e-6:
            boundaries.append(midpoint)
    boundaries.append(duration_s)
    base_spans = [
        TimeSpan(start, end)
        for start, end in zip(boundaries, boundaries[1:])
        if end > start + 1e-6
    ]
    return split_analysis_regions(
        base_spans,
        max_region_s=max_region_s,
        overlap_s=overlap_s,
    )
