from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.schema import IntroDetection, NonStoryRange, VideoProfile
from preflight.__main__ import run_preflight
from preflight.detect import sample_frames
from preflight.integrity import preflight_identity


def test_video_profile_accepts_detected_intro_range() -> None:
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=100,
        intro=IntroDetection(detected=True, start_s=0, end_s=12, confidence=0.8, reasons=["title card"]),
        non_story_ranges=[NonStoryRange(start_s=0, end_s=12, label="intro_opening", confidence=0.8)],
        classifier="heuristic",
        created_at=datetime.now(timezone.utc),
    )
    assert profile.non_story_ranges[0].label == "intro_opening"


def test_video_profile_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError):
        NonStoryRange(start_s=10, end_s=10, label="intro_opening", confidence=0.8)
    with pytest.raises(ValueError):
        IntroDetection(detected=True, start_s=10, end_s=5, confidence=0.8)


def test_preflight_identity_changes_with_film_or_config(tmp_path: Path) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film-v1")
    first = preflight_identity(film, classifier="heuristic", max_intro_s=240, sample_every_s=5, confidence_threshold=0.75, uncertain_threshold=0.55)
    changed_config = preflight_identity(film, classifier="heuristic", max_intro_s=180, sample_every_s=5, confidence_threshold=0.75, uncertain_threshold=0.55)
    film.write_bytes(b"film-v2-longer")
    changed_film = preflight_identity(film, classifier="heuristic", max_intro_s=240, sample_every_s=5, confidence_threshold=0.75, uncertain_threshold=0.55)

    assert first[1] != changed_config[1]
    assert first[0] != changed_film[0]


def test_preflight_work_cache_is_reused_only_for_matching_identity(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    work_dir = tmp_path / "work"
    sentinel_states: list[bool] = []

    def fake_build(input_path, current_work_dir, **kwargs):  # type: ignore[no-untyped-def]
        sentinel = current_work_dir / "frames" / "sentinel.jpg"
        sentinel_states.append(sentinel.exists())
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_bytes(b"frame")
        return VideoProfile(
            input_path=str(input_path),
            duration_s=10,
            intro=IntroDetection(detected=False, confidence=0, reasons=[]),
            classifier=kwargs["classifier"],
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("preflight.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("preflight.__main__.build_video_profile", fake_build)
    args = argparse.Namespace(input=film, output=tmp_path / "video_profile.json", max_intro_s=240.0, sample_every_s=5.0, classifier="heuristic", confidence_threshold=0.75, uncertain_threshold=0.55, work_dir=work_dir, force=False)

    run_preflight(args)
    run_preflight(args)
    args.max_intro_s = 180.0
    run_preflight(args)

    assert sentinel_states == [False, True, False]


def test_preflight_frame_sampling_never_seeks_exact_eof(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seek_times: list[float] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        seek_times.append(float(command[command.index("-ss") + 1]))
        Path(command[-1]).write_bytes(b"jpg")

    monkeypatch.setattr("preflight.detect.subprocess.run", fake_run)

    samples = sample_frames(
        tmp_path / "clip.mp4",
        tmp_path / "frames",
        max_intro_s=30,
        sample_every_s=5,
        duration_s=30,
    )

    assert len(samples) == 6
    assert seek_times == [0, 5, 10, 15, 20, 25]
