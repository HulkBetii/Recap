from __future__ import annotations

import argparse
from pathlib import Path

from shots.__main__ import run_shots
from shots.detect import ShotSpan
from shots.features import ShotFeatures


def make_args(tmp_path, input_path, force=False):  # type: ignore[no-untyped-def]
    return argparse.Namespace(
        input=input_path,
        output=tmp_path / "out" / "shots.json",
        thumb_dir=tmp_path / "out" / "shots",
        detector="adaptive",
        min_shot_len=0.4,
        sample_frames=5,
        face_detection="on",
        min_brightness=0.06,
        skip_intro=0.0,
        skip_outro=0.0,
        downscale="auto",
        work_dir=tmp_path / "work" / "shots",
        force=force,
        log_level="ERROR",
    )


def test_shots_cli_mock_end_to_end(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    monkeypatch.setattr("shots.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "shots.__main__.detect_shots",
        lambda *args, **kwargs: ([ShotSpan(index=0, tc_start=0.0, tc_end=1.0), ShotSpan(index=1, tc_start=1.0, tc_end=1.2)], 2.0),
    )
    monkeypatch.setattr("shots.__main__.create_face_detector", lambda mode: (object(), []))
    monkeypatch.setattr("shots.__main__.sample_frames", lambda *args, **kwargs: [object(), object()])

    def fake_features(frames, duration, config, face_detector):  # type: ignore[no-untyped-def]
        return ShotFeatures(
            motion_score=0.3,
            face_count=1,
            face_area=0.1,
            brightness=0.5,
            is_usable=duration >= config.min_shot_len,
        )

    monkeypatch.setattr("shots.__main__.compute_features_from_frames", fake_features)

    def fake_thumb(input_path, span, thumb_dir):  # type: ignore[no-untyped-def]
        thumb_dir.mkdir(parents=True, exist_ok=True)
        path = thumb_dir / f"film-{span.index:03d}.jpg"
        path.write_bytes(b"jpg")
        return path

    monkeypatch.setattr("shots.__main__.write_thumbnail", fake_thumb)

    exit_code = run_shots(make_args(tmp_path, input_path))

    assert exit_code == 0
    assert (tmp_path / "out" / "shots.json").exists()
    assert (tmp_path / "out" / "shots.meta.json").exists()
    assert (tmp_path / "work" / "shots" / "detection.json").exists()
    assert (tmp_path / "work" / "shots" / "features.json").exists()
    text = (tmp_path / "out" / "shots.json").read_text(encoding="utf-8")
    assert '"is_usable": false' in text
    assert '"face_count": 1' in text


def test_shots_cli_uses_cache_on_second_run(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    monkeypatch.setattr("shots.__main__.require_ffmpeg", lambda: None)
    detect_calls = {"count": 0}
    feature_calls = {"count": 0}

    def fake_detect(*args, **kwargs):  # type: ignore[no-untyped-def]
        detect_calls["count"] += 1
        return [ShotSpan(index=0, tc_start=0.0, tc_end=1.0)], 1.0

    monkeypatch.setattr("shots.__main__.detect_shots", fake_detect)
    monkeypatch.setattr("shots.__main__.create_face_detector", lambda mode: (object(), []))
    monkeypatch.setattr("shots.__main__.sample_frames", lambda *args, **kwargs: [object()])

    def fake_features(frames, duration, config, face_detector):  # type: ignore[no-untyped-def]
        feature_calls["count"] += 1
        return ShotFeatures(motion_score=0.1, face_count=0, face_area=0.0, brightness=0.5, is_usable=True)

    monkeypatch.setattr("shots.__main__.compute_features_from_frames", fake_features)
    monkeypatch.setattr("shots.__main__.write_thumbnail", lambda input_path, span, thumb_dir: (thumb_dir / "film-000.jpg"))
    thumb_path = tmp_path / "out" / "shots" / "film-000.jpg"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(b"jpg")

    run_shots(make_args(tmp_path, input_path))
    run_shots(make_args(tmp_path, input_path))

    assert detect_calls["count"] == 1
    assert feature_calls["count"] == 1
