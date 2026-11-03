from __future__ import annotations

import numpy as np

from shots.detect import ShotSpan
from shots.features import FeatureConfig, FrameSampleRequest, build_frame_sample_requests, compute_features_from_frames, frame_index_for_timestamp, initial_batch_position, shot_sample_times


class FakeFaceDetector:
    def detect(self, frame):  # type: ignore[no-untyped-def]
        return 2, 0.25


def test_features_mark_dark_frame_unusable() -> None:
    frames = [np.zeros((10, 10, 3), dtype=np.uint8) for _ in range(3)]
    config = FeatureConfig(sample_frames=3, face_detection="on", min_brightness=0.06, min_shot_len=0.4)

    features = compute_features_from_frames(frames, duration=1.0, config=config, face_detector=FakeFaceDetector())

    assert features.brightness == 0.0
    assert features.is_usable is False
    assert features.face_count == 2
    assert features.face_area == 0.25


def test_black_lead_is_measured_up_to_the_first_non_black_sample() -> None:
    black = np.full((10, 10, 3), 18, dtype=np.uint8)
    lit = np.full((10, 10, 3), 120, dtype=np.uint8)
    config = FeatureConfig(sample_frames=4, face_detection="off", min_brightness=0.06, min_shot_len=0.4)

    faded = compute_features_from_frames([black, black, lit, lit], duration=8.0, config=config, face_detector=FakeFaceDetector(), offsets=[0.05, 2.6, 5.4, 7.95])
    assert faded.lead_dark_s == 5.4 and faded.is_usable is True
    opening_lit = compute_features_from_frames([lit, black, lit, lit], duration=8.0, config=config, face_detector=FakeFaceDetector(), offsets=[0.05, 2.6, 5.4, 7.95])
    assert opening_lit.lead_dark_s == 0.0
    all_black = compute_features_from_frames([black] * 4, duration=8.0, config=config, face_detector=FakeFaceDetector(), offsets=[0.05, 2.6, 5.4, 7.95])
    assert all_black.lead_dark_s == 8.0
    # Without sample offsets (some frames unreadable) the lead is not measured.
    assert compute_features_from_frames([black, lit], duration=8.0, config=config, face_detector=FakeFaceDetector()).lead_dark_s == 0.0

def test_motion_score_increases_for_changed_frames() -> None:
    still = [np.full((10, 10, 3), 100, dtype=np.uint8) for _ in range(3)]
    changed = [
        np.zeros((10, 10, 3), dtype=np.uint8),
        np.full((10, 10, 3), 128, dtype=np.uint8),
        np.full((10, 10, 3), 255, dtype=np.uint8),
    ]
    config = FeatureConfig(sample_frames=3, face_detection="off", min_brightness=0.01, min_shot_len=0.4)

    still_features = compute_features_from_frames(still, duration=1.0, config=config, face_detector=FakeFaceDetector())
    changed_features = compute_features_from_frames(changed, duration=1.0, config=config, face_detector=FakeFaceDetector())

    assert changed_features.motion_score > still_features.motion_score
    assert still_features.motion_score == 0.0

def test_shot_sample_times_match_legacy_spacing() -> None:
    shot = ShotSpan(index=7, tc_start=10.0, tc_end=12.0)

    assert shot_sample_times(shot, 1) == [11.0]
    assert shot_sample_times(shot, 3) == [10.05, 11.0, 11.95]

def test_frame_sample_requests_are_sorted_and_clamped() -> None:
    spans = [ShotSpan(index=1, tc_start=0.0, tc_end=1.0), ShotSpan(index=2, tc_start=1.0, tc_end=3.0)]

    requests = build_frame_sample_requests(spans, 1, fps=10.0, frame_count=20)

    assert [(request.shot_index, request.frame_index) for request in requests] == [(1, 5), (2, 19)]
    assert frame_index_for_timestamp(9.0, fps=10.0, frame_count=20) == 19


def test_tail_batch_sampling_seeks_to_first_request() -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.positions: list[int] = []

        def set(self, _property, value):  # type: ignore[no-untyped-def]
            self.positions.append(int(value))

    capture = FakeCapture()
    requests = [FrameSampleRequest(shot_index=10, timestamp=90.0, frame_index=2700)]

    assert initial_batch_position(capture, requests, seek_to_first_request=True) == 2700
    assert capture.positions == [2700]
    assert initial_batch_position(capture, requests, seek_to_first_request=False) == 0


def test_face_detector_prefers_yunet_and_falls_back_with_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from shots import features

    detector, warnings = features.create_face_detector("on")
    assert detector.version == "opencv-yunet-2023mar"
    assert warnings == []

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("model missing")

    monkeypatch.setattr(features.YuNetFaceDetector, "__init__", broken)
    detector, warnings = features.create_face_detector("on")
    assert detector.version == "opencv-haar-frontalface-default"
    assert warnings and "YuNetFaceDetector unavailable" in warnings[0]


def test_yunet_detects_nothing_on_blank_frame() -> None:
    from shots.features import YuNetFaceDetector

    assert YuNetFaceDetector().detect(np.zeros((202, 360, 3), dtype=np.uint8)) == (0, 0.0)


def test_feature_cache_key_tracks_the_loaded_detector() -> None:
    import argparse

    from shots.__main__ import feature_cache_key
    from shots.detect import ShotSpan

    args = argparse.Namespace(sample_frames=3, frame_sampling="batch", face_detection="on", min_brightness=0.1, min_shot_len=0.5)
    spans = [ShotSpan(index=0, tc_start=0.0, tc_end=2.0)]
    assert feature_cache_key(spans, args, face_detector="off") != feature_cache_key(spans, args, face_detector="opencv-yunet-2023mar")
