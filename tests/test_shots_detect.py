from __future__ import annotations

from pathlib import Path

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
