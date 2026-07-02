from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, require_ffmpeg
from common.schema import Shot, ShotsMeta, validate_shots, write_json
from shots.cache import ShotsCache, stable_hash
from shots.detect import ShotSpan, detect_shots
from shots.features import FeatureConfig, compute_features_from_frames, create_face_detector, sample_frames
from shots.thumbs import write_thumbnail

DEFAULT_MIN_SHOT_LEN = 0.4
DEFAULT_SAMPLE_FRAMES = 5
DEFAULT_MIN_BRIGHTNESS = 0.06


class ShotsError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 4 shots: film.mp4 -> shots.json + thumbnails")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--thumb-dir", default=Path("shots"), type=Path)
    parser.add_argument("--detector", default="adaptive", choices=["adaptive", "content"])
    parser.add_argument("--min-shot-len", default=DEFAULT_MIN_SHOT_LEN, type=float)
    parser.add_argument("--sample-frames", default=DEFAULT_SAMPLE_FRAMES, type=int)
    parser.add_argument("--face-detection", default="on", choices=["on", "off"])
    parser.add_argument("--min-brightness", default=DEFAULT_MIN_BRIGHTNESS, type=float)
    parser.add_argument("--skip-intro", default=0.0, type=float)
    parser.add_argument("--skip-outro", default=0.0, type=float)
    parser.add_argument("--downscale", default="auto")
    parser.add_argument("--work-dir", default=Path("work/shots"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def input_signature(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def detection_cache_key(input_path: Path, args: argparse.Namespace) -> str:
    return stable_hash({
        "input": input_signature(input_path),
        "detector": args.detector,
        "skip_intro": args.skip_intro,
        "skip_outro": args.skip_outro,
        "downscale": args.downscale,
    })


def feature_cache_key(spans: list[ShotSpan], args: argparse.Namespace) -> str:
    return stable_hash({
        "spans": [span.__dict__ for span in spans],
        "sample_frames": args.sample_frames,
        "face_detection": args.face_detection,
        "min_brightness": args.min_brightness,
        "min_shot_len": args.min_shot_len,
    })


def spans_to_json(spans: list[ShotSpan]) -> list[dict[str, float | int]]:
    return [{"index": span.index, "tc_start": span.tc_start, "tc_end": span.tc_end} for span in spans]


def spans_from_json(data: list[dict]) -> list[ShotSpan]:
    return [ShotSpan(index=int(item["index"]), tc_start=float(item["tc_start"]), tc_end=float(item["tc_end"])) for item in data]


def run_shots(args: argparse.Namespace) -> int:
    logger = logging.getLogger("shots")
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    thumb_dir = args.thumb_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    if not input_path.is_file():
        raise ShotsError(f"input video does not exist: {input_path}")
    if args.min_shot_len <= 0:
        raise ShotsError("--min-shot-len must be > 0")
    if args.sample_frames <= 0:
        raise ShotsError("--sample-frames must be > 0")
    if not 0 <= args.min_brightness <= 1:
        raise ShotsError("--min-brightness must be between 0 and 1")
    require_ffmpeg()
    cache = ShotsCache(work_dir, force=args.force)
    cache.prepare()

    logger.info("[1/4] Detecting shots")
    detect_key = detection_cache_key(input_path, args)
    cached_detection = cache.read_cached("detection.json", detect_key)
    if cached_detection is not None:
        spans = spans_from_json(cached_detection["spans"])
        duration_s = float(cached_detection["duration_s"])
    else:
        spans, duration_s = detect_shots(
            input_path,
            detector=args.detector,
            skip_intro=args.skip_intro,
            skip_outro=args.skip_outro,
            downscale=args.downscale,
        )
        cache.write_cached("detection.json", detect_key, {"duration_s": duration_s, "spans": spans_to_json(spans)})

    logger.info("[2/4] Computing features")
    warnings: list[str] = []
    face_detector, face_warnings = create_face_detector(args.face_detection)
    warnings.extend(face_warnings)
    config = FeatureConfig(
        sample_frames=args.sample_frames,
        face_detection=args.face_detection,
        min_brightness=args.min_brightness,
        min_shot_len=args.min_shot_len,
    )
    feature_key = feature_cache_key(spans, args)
    cached_features = cache.read_cached("features.json", feature_key)
    features_by_index: dict[int, dict]
    if cached_features is not None:
        features_by_index = {int(key): value for key, value in cached_features.items()}
    else:
        features_by_index = {}
        for span in spans:
            frames = sample_frames(input_path, span, args.sample_frames)
            features = compute_features_from_frames(frames, duration=span.duration, config=config, face_detector=face_detector)
            features_by_index[span.index] = features.__dict__
        cache.write_cached("features.json", feature_key, {str(key): value for key, value in features_by_index.items()})

    logger.info("[3/4] Writing thumbnails")
    shots: list[Shot] = []
    for span in spans:
        thumb_path = write_thumbnail(input_path, span, thumb_dir)
        rel_thumb = thumb_path.relative_to(output_path.parent).as_posix() if thumb_path.is_relative_to(output_path.parent) else thumb_path.as_posix()
        features = features_by_index[span.index]
        shots.append(
            Shot(
                src=input_path.name,
                index=span.index,
                tc_start=span.tc_start,
                tc_end=span.tc_end,
                duration=round(span.duration, 3),
                thumb=rel_thumb,
                motion_score=float(features["motion_score"]),
                face_count=int(features["face_count"]),
                face_area=float(features["face_area"]),
                brightness=float(features["brightness"]),
                is_usable=bool(features["is_usable"]),
            )
        )
    shots = validate_shots(shots, duration_s)

    logger.info("[4/4] Writing shots output")
    write_json(output_path, shots)
    meta = ShotsMeta(
        src=str(input_path),
        duration_s=duration_s,
        n_shots=len(shots),
        n_usable=sum(1 for shot in shots if shot.is_usable),
        detector=args.detector,
        feature_config={
            "sample_frames": args.sample_frames,
            "face_detection": args.face_detection,
            "min_brightness": args.min_brightness,
            "min_shot_len": args.min_shot_len,
            "skip_intro": args.skip_intro,
            "skip_outro": args.skip_outro,
            "downscale": args.downscale,
        },
        model_versions={"face_detector": "opencv-haar-frontalface-default" if args.face_detection == "on" else "off"},
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
        warnings=warnings,
    )
    write_json(output_path.with_name("shots.meta.json"), meta)
    print(f"shots: total={len(shots)} usable={meta.n_usable} face_shots={sum(1 for shot in shots if shot.face_count > 0)}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_shots(args)
    except (ShotsError, ValueError, RuntimeError, MediaError, json.JSONDecodeError) as exc:
        parser.exit(2, f"shots: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
