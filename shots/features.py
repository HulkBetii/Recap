from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np

from shots.detect import ShotSpan

TRANSITION_SPIKE_THRESHOLD = 0.92
DEFAULT_FRAME_SAMPLE_WIDTH = 360


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

@dataclass(frozen=True)
class FrameSampleRequest:
    shot_index: int
    timestamp: float
    frame_index: int

@dataclass(frozen=True)
class SampledFrame:
    shot_index: int
    timestamp: float
    frame: np.ndarray


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


def shot_sample_times(shot: ShotSpan, sample_count: int) -> list[float]:
    count = max(1, sample_count)
    if count == 1:
        return [shot.tc_start + shot.duration / 2]
    margin = min(0.05, shot.duration / 4)
    return np.linspace(shot.tc_start + margin, shot.tc_end - margin, count).tolist()

def frame_index_for_timestamp(timestamp: float, *, fps: float, frame_count: int = 0) -> int:
    if fps <= 0:
        raise ValueError("fps must be > 0")
    frame_index = max(0, int(round(max(0.0, timestamp) * fps)))
    if frame_count > 0:
        frame_index = min(frame_index, frame_count - 1)
    return frame_index

def build_frame_sample_requests(
    spans: list[ShotSpan],
    sample_count: int,
    *,
    fps: float,
    frame_count: int = 0,
) -> list[FrameSampleRequest]:
    requests: list[FrameSampleRequest] = []
    for span in sorted(spans, key=lambda item: (item.tc_start, item.index)):
        for timestamp in shot_sample_times(span, sample_count):
            requests.append(
                FrameSampleRequest(
                    shot_index=span.index,
                    timestamp=float(timestamp),
                    frame_index=frame_index_for_timestamp(timestamp, fps=fps, frame_count=frame_count),
                )
            )
    return requests

def sample_frames(
    input_path: Path,
    shot: ShotSpan,
    sample_count: int,
    max_width: int = DEFAULT_FRAME_SAMPLE_WIDTH,
) -> list[np.ndarray]:
    import cv2

    cap = cv2.VideoCapture(str(input_path))
    frames: list[np.ndarray] = []
    try:
        for timestamp in shot_sample_times(shot, sample_count):
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frames.append(resize_frame(frame, max_width=max_width))
    finally:
        cap.release()
    return frames

def iter_batch_sampled_frames(
    input_path: Path,
    spans: list[ShotSpan],
    sample_count: int,
    max_width: int = DEFAULT_FRAME_SAMPLE_WIDTH,
) -> Iterator[tuple[ShotSpan, list[SampledFrame]]]:
    import cv2

    ordered_spans = sorted(spans, key=lambda item: (item.tc_start, item.index))
    if not ordered_spans:
        return
    cap = cv2.VideoCapture(str(input_path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0:
            cap.release()
            for span in ordered_spans:
                yield (
                    span,
                    [
                        SampledFrame(shot_index=span.index, timestamp=timestamp, frame=frame)
                        for timestamp, frame in zip(shot_sample_times(span, sample_count), sample_frames(input_path, span, sample_count, max_width=max_width))
                    ],
                )
            return
        requests = build_frame_sample_requests(ordered_spans, sample_count, fps=fps, frame_count=frame_count)
        current_shot = ordered_spans[0].index
        current_samples: list[SampledFrame] = []
        span_cursor = 0
        current_position = 0
        for request, frame, current_position in _iter_request_frames(
            cap,
            requests,
            current_position=current_position,
            max_width=max_width,
        ):
            if request.shot_index != current_shot:
                while span_cursor < len(ordered_spans) and ordered_spans[span_cursor].index != current_shot:
                    yield ordered_spans[span_cursor], []
                    span_cursor += 1
                if span_cursor < len(ordered_spans):
                    yield ordered_spans[span_cursor], current_samples
                    span_cursor += 1
                while span_cursor < len(ordered_spans) and ordered_spans[span_cursor].index != request.shot_index:
                    yield ordered_spans[span_cursor], []
                    span_cursor += 1
                current_shot = request.shot_index
                current_samples = []
            if frame is not None:
                current_samples.append(SampledFrame(shot_index=request.shot_index, timestamp=request.timestamp, frame=frame))
        while span_cursor < len(ordered_spans) and ordered_spans[span_cursor].index != current_shot:
            yield ordered_spans[span_cursor], []
            span_cursor += 1
        if span_cursor < len(ordered_spans):
            yield ordered_spans[span_cursor], current_samples
            span_cursor += 1
        while span_cursor < len(ordered_spans):
            yield ordered_spans[span_cursor], []
            span_cursor += 1
    finally:
        cap.release()

def _iter_request_frames(
    cap,  # type: ignore[no-untyped-def]
    requests: list[FrameSampleRequest],
    *,
    current_position: int,
    max_width: int,
) -> Iterator[tuple[FrameSampleRequest, np.ndarray | None, int]]:
    index = 0
    while index < len(requests):
        frame_index = requests[index].frame_index
        group: list[FrameSampleRequest] = []
        while index < len(requests) and requests[index].frame_index == frame_index:
            group.append(requests[index])
            index += 1
        frame, current_position = _read_frame_at_index(cap, frame_index, current_position=current_position, max_width=max_width)
        for request in group:
            yield request, frame, current_position

def _read_frame_at_index(
    cap,  # type: ignore[no-untyped-def]
    frame_index: int,
    *,
    current_position: int,
    max_width: int,
) -> tuple[np.ndarray | None, int]:
    import cv2

    if frame_index < current_position:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        current_position = frame_index
    while current_position < frame_index:
        ok = cap.grab()
        if not ok:
            return None, current_position
        current_position += 1
    ok, frame = cap.read()
    current_position += 1
    if not ok or frame is None:
        return None, current_position
    return resize_frame(frame, max_width=max_width), current_position

def resize_frame(frame: np.ndarray, *, max_width: int) -> np.ndarray:
    if max_width > 0 and frame.shape[1] > max_width:
        import cv2

        scale = max_width / frame.shape[1]
        return cv2.resize(frame, (max_width, max(1, int(frame.shape[0] * scale))))
    return frame


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
