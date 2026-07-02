from __future__ import annotations

from pathlib import Path

from shots.detect import ShotSpan


def thumbnail_path(input_path: Path, thumb_dir: Path, index: int) -> Path:
    return thumb_dir / f"{input_path.stem}-{index:03d}.jpg"


def write_thumbnail(input_path: Path, shot: ShotSpan, thumb_dir: Path) -> Path:
    import cv2

    thumb_dir.mkdir(parents=True, exist_ok=True)
    output_path = thumbnail_path(input_path, thumb_dir, shot.index)
    cap = cv2.VideoCapture(str(input_path))
    try:
        midpoint_ms = (shot.tc_start + (shot.duration / 2)) * 1000
        cap.set(cv2.CAP_PROP_POS_MSEC, midpoint_ms)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"Could not read thumbnail frame for shot #{shot.index}")
        cv2.imwrite(str(output_path), frame)
    finally:
        cap.release()
    return output_path
