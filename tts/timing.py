from __future__ import annotations

from pathlib import Path

from common.schema import BeatTiming, validate_beats_timing


def build_timings(beat_ids: list[int], audio_paths: list[Path], durations: list[float], pause_s: float) -> list[BeatTiming]:
    timings: list[BeatTiming] = []
    cursor = 0.0
    for index, beat_id in enumerate(beat_ids):
        duration = round(durations[index], 3)
        start = round(cursor, 3)
        end = round(start + duration, 3)
        timings.append(
            BeatTiming(
                beat_id=beat_id,
                audio_path=audio_paths[index].as_posix(),
                tl_start=start,
                tl_end=end,
                duration=duration,
            )
        )
        cursor = end + pause_s
    return validate_beats_timing(timings, pause_s)
