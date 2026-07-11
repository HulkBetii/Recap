from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from common.media import extract_frame


@dataclass(frozen=True)
class FrameRequest:
    timestamp: float
    output_path: Path


def extract_keyframes(film_path: Path, requests: list[FrameRequest], *, mode: str) -> None:
    if mode == "per-frame":
        for request in requests:
            extract_frame(film_path, request.timestamp, request.output_path)
        return
    if mode != "batch":
        raise ValueError("frame sampling mode must be per-frame or batch")
    extract_keyframes_batch(film_path, requests)


def extract_keyframes_batch(film_path: Path, requests: list[FrameRequest]) -> None:
    import cv2

    ordered = sorted(requests, key=lambda item: item.timestamp)
    if not ordered:
        return
    cap = cv2.VideoCapture(str(film_path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0:
            for request in ordered:
                extract_frame(film_path, request.timestamp, request.output_path)
            return
        current_position = 0
        cached_index = -1
        cached_frame = None
        for request in ordered:
            frame_index = max(0, int(round(request.timestamp * fps)))
            if frame_count > 0:
                frame_index = min(frame_index, frame_count - 1)
            if frame_index != cached_index:
                if frame_index < current_position:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    current_position = frame_index
                while current_position < frame_index:
                    if not cap.grab():
                        raise RuntimeError(f"Could not seek keyframe at {request.timestamp:.3f}s")
                    current_position += 1
                ok, cached_frame = cap.read()
                current_position += 1
                cached_index = frame_index
                if not ok or cached_frame is None:
                    raise RuntimeError(f"Could not read keyframe at {request.timestamp:.3f}s")
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(request.output_path), cached_frame):
                raise RuntimeError(f"Could not write keyframe: {request.output_path}")
    finally:
        cap.release()
