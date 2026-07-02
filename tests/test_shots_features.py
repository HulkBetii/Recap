from __future__ import annotations

import numpy as np

from shots.features import FeatureConfig, compute_features_from_frames


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
