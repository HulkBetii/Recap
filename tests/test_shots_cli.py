from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.schema import Shot
from shots.__main__ import clamp_shots_to_duration, run_shots
from shots.detect import ShotSpan
from shots.features import SampledFrame, ShotFeatures


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


def test_clamp_shots_to_duration_repairs_sub_millisecond_rounding_overflow() -> None:
    shot = Shot(
        src="film.mp4",
        index=0,
        tc_start=6959.0,
        tc_end=6960.811,
        duration=1.811,
        thumb="shot.jpg",
        motion_score=0.1,
        face_count=0,
        face_area=0,
        brightness=0.2,
        is_usable=True,
    )
    clamped = clamp_shots_to_duration([shot], 6960.810667)
    assert clamped[0].tc_end == 6960.810667
    assert clamped[0].duration == 1.811


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

def test_shots_cli_batch_sampling_writes_thumbnails_from_samples(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    args = make_args(tmp_path, input_path)
    args.frame_sampling = "batch"
    monkeypatch.setattr("shots.__main__.require_ffmpeg", lambda: None)
    spans = [ShotSpan(index=0, tc_start=0.0, tc_end=1.0), ShotSpan(index=1, tc_start=1.0, tc_end=2.0)]
    monkeypatch.setattr("shots.__main__.detect_shots", lambda *args, **kwargs: (spans, 2.0))
    monkeypatch.setattr("shots.__main__.create_face_detector", lambda mode: (object(), []))
    per_shot_calls = {"count": 0}
    batch_calls = {"count": 0}
    thumb_calls: list[int] = []

    def fake_sample_frames(*args, **kwargs):  # type: ignore[no-untyped-def]
        per_shot_calls["count"] += 1
        return []

    def fake_batch_frames(input_path, spans, sample_count, max_width):  # type: ignore[no-untyped-def]
        batch_calls["count"] += 1
        for span in spans:
            yield span, [SampledFrame(shot_index=span.index, timestamp=span.tc_start + 0.5, frame=object())]

    def fake_features(frames, duration, config, face_detector):  # type: ignore[no-untyped-def]
        return ShotFeatures(motion_score=0.2, face_count=0, face_area=0.0, brightness=0.4, is_usable=True)

    def fake_thumb_from_frame(input_path, thumb_dir, index, frame):  # type: ignore[no-untyped-def]
        thumb_calls.append(index)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        path = thumb_dir / f"film-{index:03d}.jpg"
        path.write_bytes(b"jpg")
        return path

    monkeypatch.setattr("shots.__main__.sample_frames", fake_sample_frames)
    monkeypatch.setattr("shots.__main__.iter_batch_sampled_frames", fake_batch_frames)
    monkeypatch.setattr("shots.__main__.compute_features_from_frames", fake_features)
    monkeypatch.setattr("shots.__main__.write_thumbnail_from_frame", fake_thumb_from_frame)
    monkeypatch.setattr("shots.__main__.write_thumbnail", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("per-shot thumbnail should not run")))

    exit_code = run_shots(args)

    assert exit_code == 0
    assert per_shot_calls["count"] == 0
    assert batch_calls["count"] == 1
    assert thumb_calls == [0, 1]
    meta = (tmp_path / "out" / "shots.meta.json").read_text(encoding="utf-8")
    assert '"frame_sampling": "batch"' in meta


def test_shots_cli_end_credit_marking_has_separate_cache(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    args = make_args(tmp_path, input_path)
    args.end_credit_guard = True
    args.end_credit_tail_s = 100.0
    args.end_credit_threshold = 0.6
    spans = [ShotSpan(index=0, tc_start=0.0, tc_end=1.0), ShotSpan(index=1, tc_start=900.0, tc_end=901.0)]
    detect_calls = {"count": 0}
    feature_calls = {"count": 0}
    marking_calls = {"count": 0}

    def fake_detect(*args, **kwargs):  # type: ignore[no-untyped-def]
        detect_calls["count"] += 1
        return spans, 1000.0

    def fake_features(frames, duration, config, face_detector):  # type: ignore[no-untyped-def]
        feature_calls["count"] += 1
        return ShotFeatures(motion_score=0.1, face_count=0, face_area=0.0, brightness=0.5, is_usable=True)

    def fake_tail_frames(input_path, tail_spans, sample_count, max_width, *, seek_to_first_request=False):  # type: ignore[no-untyped-def]
        marking_calls["count"] += 1
        assert [span.index for span in tail_spans] == [1]
        assert seek_to_first_request is True
        yield tail_spans[0], [SampledFrame(shot_index=1, timestamp=900.5, frame=object())]

    def fake_thumb(input_path, span, thumb_dir):  # type: ignore[no-untyped-def]
        thumb_dir.mkdir(parents=True, exist_ok=True)
        path = thumb_dir / f"film-{span.index:03d}.jpg"
        path.write_bytes(b"jpg")
        return path

    monkeypatch.setattr("shots.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("shots.__main__.detect_shots", fake_detect)
    monkeypatch.setattr("shots.__main__.create_face_detector", lambda mode: (object(), []))
    monkeypatch.setattr("shots.__main__.sample_frames", lambda *args, **kwargs: [object()])
    monkeypatch.setattr("shots.__main__.compute_features_from_frames", fake_features)
    monkeypatch.setattr("shots.__main__.iter_batch_sampled_frames", fake_tail_frames)
    monkeypatch.setattr("shots.__main__.credit_like_score", lambda frames: 1.0)
    monkeypatch.setattr("shots.__main__.write_thumbnail", fake_thumb)

    run_shots(args)
    run_shots(args)
    args.end_credit_threshold = 0.8
    run_shots(args)

    assert detect_calls["count"] == 1
    assert feature_calls["count"] == 2
    assert marking_calls["count"] == 2
    assert (tmp_path / "work" / "shots" / "end_credit_marking.json").exists()
    shots = [Shot.model_validate(item) for item in json.loads((tmp_path / "out" / "shots.json").read_text(encoding="utf-8"))]
    assert shots[0].is_end_credit is False
    assert shots[1].is_end_credit is True
