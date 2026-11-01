from __future__ import annotations

from pathlib import Path
import subprocess

import shots.detect as detect


def test_run_pyscenedetect_falls_back_to_video_manager(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fail_open_video(input_path: Path, *, detector: str, downscale: str):  # type: ignore[no-untyped-def]
        raise ImportError("open_video unavailable")

    def fake_video_manager(input_path: Path, *, detector: str, downscale: str):  # type: ignore[no-untyped-def]
        assert detector == "adaptive"
        assert downscale == "auto"
        return [(0.0, 1.0)]

    monkeypatch.setattr(detect, "run_pyscenedetect_open_video", fail_open_video)
    monkeypatch.setattr(detect, "run_pyscenedetect_video_manager", fake_video_manager)

    assert detect.run_pyscenedetect(Path("film.mp4"), detector="adaptive", downscale="auto") == [(0.0, 1.0)]

def test_boundaries_to_scenes_dedupes_close_boundaries() -> None:
    scenes = detect.boundaries_to_scenes([0.1, 1.0, 1.1, 3.5, 9.9, 11.0], duration=10.0, min_gap=0.3)

    assert scenes == [(0.0, 1.0), (1.0, 3.5), (3.5, 9.9), (9.9, 10.0)]

def test_split_long_scenes_keeps_parts_under_limit() -> None:
    scenes = detect.split_long_scenes([(0.0, 21.0), (30.0, 34.0)], max_shot_len=8.0)

    assert scenes == [(0.0, 7.0), (7.0, 14.0), (14.0, 21.0), (30.0, 34.0)]

def test_run_ffmpeg_scene_parses_showinfo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("text") is not True
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b"",
            stderr=(
                b"\x8d"
                b"[Parsed_showinfo_2] n:   0 pts:    885 pts_time:29.5 duration_time:0.0333333\n"
                b"[Parsed_showinfo_2] n:   1 pts:   1322 pts_time:44.066667 duration_time:0.0333333\n"
            ),
        )

    monkeypatch.setattr(detect.subprocess, "run", fake_run)

    scenes = detect.run_ffmpeg_scene(Path("film.mp4"), duration=60.0, threshold=0.3, scale_width=640, min_gap=0.3)

    assert scenes == [(0.0, 29.5), (29.5, 44.067), (44.067, 60.0)]


def test_detect_shots_clamps_rounded_final_end_to_source_duration(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    duration = 6960.810667
    monkeypatch.setattr(detect, "probe_duration", lambda _path: duration)
    monkeypatch.setattr(detect, "run_ffmpeg_scene", lambda *args, **kwargs: [(0.0, duration)])

    spans, detected_duration = detect.detect_shots(
        Path("film.mp4"),
        detector="ffmpeg-scene",
        skip_intro=0.0,
        skip_outro=0.0,
        downscale="auto",
    )

    assert detected_duration == duration
    assert spans[-1].tc_end == duration
    assert spans[-1].tc_end <= detected_duration
