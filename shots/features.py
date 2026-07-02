from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from shots.detect import ShotSpan

TRANSITION_SPIKE_THRESHOLD = 0.92


@dataclass(frozen=True)
class FeatureConfig:
    sample_frames: int
    face_detection: str
    min_brightness: float
    min_shot_len: float


@dataclass(frozen=True)
class ShotFeatures:
    motion_score: float
    face_count: int
    face_area: float
    brightness: float
    is_usable: bool


class FaceDetector(Protocol):
    def detect(self, frame: np.ndarray) -> tuple[int, float]:
        ...


class NoFaceDetector:
    def detect(self, frame: np.ndarray) -> tuple[int, float]:
        return 0, 0.0


class HaarFaceDetector:
    def __init__(self) -> None:
        import cv2

        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.exists():
            raise RuntimeError(f"Haar cascade not found: {cascade_path}")
        self.cv2 = cv2
        self.classifier = cv2.CascadeClassifier(str(cascade_path))
        if self.classifier.empty():
            raise RuntimeError(f"Could not load Haar cascade: {cascade_path}")

    def detect(self, frame: np.ndarray) -> tuple[int, float]:
        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        faces = self.classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        frame_area = float(frame.shape[0] * frame.shape[1])
        max_area = 0.0
        for (_x, _y, width, height) in faces:
            max_area = max(max_area, (float(width) * float(height)) / frame_area)
        return int(len(faces)), min(1.0, max_area)


def create_face_detector(mode: str) -> tuple[FaceDetector, list[str]]:
    if mode == "off":
        return NoFaceDetector(), []
    if mode != "on":
        raise ValueError("--face-detection must be on or off")
    try:
        return HaarFaceDetector(), []
    except Exception as exc:  # noqa: BLE001
        return NoFaceDetector(), [f"Face detection disabled: {exc}"]


def sample_frames(input_path: Path, shot: ShotSpan, sample_count: int, max_width: int = 360) -> list[np.ndarray]:
    import cv2

    cap = cv2.VideoCapture(str(input_path))
    frames: list[np.ndarray] = []
    try:
        count = max(1, sample_count)
        if count == 1:
            times = [shot.tc_start + shot.duration / 2]
        else:
            margin = min(0.05, shot.duration / 4)
            times = np.linspace(shot.tc_start + margin, shot.tc_end - margin, count).tolist()
        for timestamp in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            if frame.shape[1] > max_width:
                scale = max_width / frame.shape[1]
                frame = cv2.resize(frame, (max_width, max(1, int(frame.shape[0] * scale))))
            frames.append(frame)
    finally:
        cap.release()
    return frames


def compute_features_from_frames(
    frames: list[np.ndarray],
    *,
    duration: float,
    config: FeatureConfig,
    face_detector: FaceDetector,
) -> ShotFeatures:
    if not frames:
        return ShotFeatures(motion_score=0.0, face_count=0, face_area=0.0, brightness=0.0, is_usable=False)
    grays = [to_luma(frame) for frame in frames]
    brightness = float(np.mean([gray.mean() / 255.0 for gray in grays]))
    diffs: list[float] = []
    for prev, curr in zip(grays, grays[1:]):
        if prev.shape != curr.shape:
            continue
        diffs.append(float(np.mean(np.abs(curr.astype(np.float32) - prev.astype(np.float32))) / 255.0))
    motion = float(np.mean(diffs)) if diffs else 0.0
    max_face_count = 0
    max_face_area = 0.0
    for frame in frames:
        count, area = face_detector.detect(frame)
        max_face_count = max(max_face_count, count)
        max_face_area = max(max_face_area, area)
    is_usable = (
        duration >= config.min_shot_len
        and brightness >= config.min_brightness
        and motion < TRANSITION_SPIKE_THRESHOLD
    )
    return ShotFeatures(
        motion_score=round(clamp01(motion), 4),
        face_count=max_face_count,
        face_area=round(clamp01(max_face_area), 4),
        brightness=round(clamp01(brightness), 4),
        is_usable=is_usable,
    )


def to_luma(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    blue = frame[:, :, 0].astype(np.float32)
    green = frame[:, :, 1].astype(np.float32)
    red = frame[:, :, 2].astype(np.float32)
    return (0.114 * blue + 0.587 * green + 0.299 * red).astype(np.uint8)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
