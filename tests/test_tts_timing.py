from __future__ import annotations

from pathlib import Path

from tts.timing import build_timings


def test_build_timings_accumulates_measured_durations_and_pause() -> None:
    timings = build_timings(
        [0, 1, 2],
        [Path("audio/0.mp3"), Path("audio/1.mp3"), Path("audio/2.mp3")],
        [1.0, 2.0, 1.5],
        pause_s=0.2,
    )

    assert [(t.tl_start, t.tl_end, t.duration) for t in timings] == [(0.0, 1.0, 1.0), (1.2, 3.2, 2.0), (3.4, 4.9, 1.5)]
