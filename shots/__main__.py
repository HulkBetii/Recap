from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, require_ffmpeg
from common.schema import Shot, ShotsMeta, VideoProfile, validate_shots, write_json
from shots.cache import ShotsCache, stable_hash
from shots.detect import ShotSpan, detect_shots
from shots.features import (
    DEFAULT_FRAME_SAMPLE_WIDTH,
    FaceDetector,
    FeatureConfig,
    SampledFrame,
    compute_features_from_frames,
    create_face_detector,
    iter_batch_sampled_frames,
    sample_frames,
)
from shots.profile import apply_video_profile_to_shots, profile_cache_key, video_profile_hash
from shots.thumbs import thumbnail_path, write_thumbnail, write_thumbnail_from_frame

DEFAULT_MIN_SHOT_LEN = 0.4
DEFAULT_SAMPLE_FRAMES = 5
DEFAULT_MIN_BRIGHTNESS = 0.06
DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_SCENE_SCALE_WIDTH = 640
DEFAULT_SCENE_MIN_GAP = 0.3
DEFAULT_FRAME_SAMPLING = "per-shot"


class ShotsError(RuntimeError):
    pass



def load_video_profile(path: Path | None) -> VideoProfile | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ShotsError(f"video profile does not exist: {resolved}")
    return VideoProfile.model_validate_json(resolved.read_text(encoding="utf-8"))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 4 shots: film.mp4 -> shots.json + thumbnails")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--thumb-dir", default=Path("shots"), type=Path)
    parser.add_argument("--detector", default="adaptive", choices=["adaptive", "content", "ffmpeg-scene"])
    parser.add_argument("--min-shot-len", default=DEFAULT_MIN_SHOT_LEN, type=float)
    parser.add_argument("--sample-frames", default=DEFAULT_SAMPLE_FRAMES, type=int)
    parser.add_argument("--frame-sampling", default=DEFAULT_FRAME_SAMPLING, choices=["per-shot", "batch"])
    parser.add_argument("--face-detection", default="on", choices=["on", "off"])
    parser.add_argument("--min-brightness", default=DEFAULT_MIN_BRIGHTNESS, type=float)
    parser.add_argument("--scene-threshold", default=DEFAULT_SCENE_THRESHOLD, type=float, help="ffmpeg-scene detector threshold")
    parser.add_argument("--scene-scale-width", default=DEFAULT_SCENE_SCALE_WIDTH, type=int, help="Scale width for ffmpeg-scene; 0 disables scaling")
    parser.add_argument("--scene-min-gap", default=DEFAULT_SCENE_MIN_GAP, type=float, help="Minimum seconds between ffmpeg-scene boundaries")
    parser.add_argument("--max-shot-len", default=0.0, type=float, help="Split detected scenes longer than this many seconds; 0 disables")
    parser.add_argument("--skip-intro", default=0.0, type=float, help="Debug override only; default pipeline should use --video-profile")
    parser.add_argument("--video-profile", default=None, type=Path)
    parser.add_argument("--skip-outro", default=0.0, type=float)
    parser.add_argument("--downscale", default="auto")
    parser.add_argument("--work-dir", default=Path("work/shots"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--profile-only", action="store_true", help="Debug: re-apply --video-profile from cached detection/features only")
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
        "scene_threshold": args.scene_threshold,
        "scene_scale_width": args.scene_scale_width,
        "scene_min_gap": args.scene_min_gap,
        "max_shot_len": args.max_shot_len,
    })



def legacy_detection_cache_key(input_path: Path, args: argparse.Namespace) -> str:
    return stable_hash({
        "input": input_signature(input_path),
        "detector": args.detector,
        "skip_intro": args.skip_intro,
        "skip_outro": args.skip_outro,
        "downscale": args.downscale,
        "scene_threshold": getattr(args, "scene_threshold", DEFAULT_SCENE_THRESHOLD),
        "scene_scale_width": getattr(args, "scene_scale_width", DEFAULT_SCENE_SCALE_WIDTH),
        "scene_min_gap": getattr(args, "scene_min_gap", DEFAULT_SCENE_MIN_GAP),
        "max_shot_len": getattr(args, "max_shot_len", 0.0),
        "video_profile": str(args.video_profile) if args.video_profile else None,
    })

def feature_cache_key(spans: list[ShotSpan], args: argparse.Namespace) -> str:
    return stable_hash({
        "spans": [span.__dict__ for span in spans],
        "sample_frames": args.sample_frames,
        "frame_sampling": args.frame_sampling,
        "face_detection": args.face_detection,
        "min_brightness": args.min_brightness,
        "min_shot_len": args.min_shot_len,
    })


def spans_to_json(spans: list[ShotSpan]) -> list[dict[str, float | int]]:
    return [{"index": span.index, "tc_start": span.tc_start, "tc_end": span.tc_end} for span in spans]


def spans_from_json(data: list[dict]) -> list[ShotSpan]:
    return [ShotSpan(index=int(item["index"]), tc_start=float(item["tc_start"]), tc_end=float(item["tc_end"])) for item in data]

def features_to_shots(input_path: Path, output_path: Path, thumb_dir: Path, spans: list[ShotSpan], features_by_index: dict[int, dict]) -> list[Shot]:
    shots: list[Shot] = []
    for span in spans:
        thumb_path = thumb_dir / f"{input_path.stem}-{span.index:03d}.jpg"
        if not thumb_path.exists():
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
    return shots


def clamp_shots_to_duration(shots: list[Shot], duration_s: float) -> list[Shot]:
    output: list[Shot] = []
    for shot in shots:
        tc_start = min(shot.tc_start, duration_s)
        tc_end = min(shot.tc_end, duration_s)
        if tc_end <= tc_start:
            continue
        output.append(
            shot.model_copy(
                update={
                    "index": len(output),
                    "tc_start": tc_start,
                    "tc_end": tc_end,
                    "duration": round(tc_end - tc_start, 3),
                }
            )
        )
    return output

def thumbnail_sample(span: ShotSpan, samples: list[SampledFrame]) -> SampledFrame | None:
    if not samples:
        return None
    midpoint = span.tc_start + (span.duration / 2)
    return min(samples, key=lambda sample: abs(sample.timestamp - midpoint))

def compute_features_per_shot(
    *,
    input_path: Path,
    thumb_dir: Path,
    spans: list[ShotSpan],
    args: argparse.Namespace,
    config: FeatureConfig,
    face_detector: FaceDetector,
    logger: logging.Logger,
) -> dict[int, dict]:
    features_by_index: dict[int, dict] = {}
    if args.frame_sampling == "batch":
        total = len(spans)
        for count, (span, samples) in enumerate(
            iter_batch_sampled_frames(input_path, spans, args.sample_frames, max_width=DEFAULT_FRAME_SAMPLE_WIDTH),
            start=1,
        ):
            frames = [sample.frame for sample in samples]
            features = compute_features_from_frames(frames, duration=span.duration, config=config, face_detector=face_detector)
            features_by_index[span.index] = features.__dict__
            thumb = thumbnail_sample(span, samples)
            thumb_output = thumbnail_path(input_path, thumb_dir, span.index)
            if thumb is not None and not thumb_output.exists():
                write_thumbnail_from_frame(input_path, thumb_dir, span.index, thumb.frame)
            if count % 100 == 0 or count == total:
                logger.info("[2/4] Batch sampled %s/%s shots", count, total)
        return features_by_index

    for span in spans:
        frames = sample_frames(input_path, span, args.sample_frames)
        features = compute_features_from_frames(frames, duration=span.duration, config=config, face_detector=face_detector)
        features_by_index[span.index] = features.__dict__
    return features_by_index


def run_shots(args: argparse.Namespace) -> int:
    logger = logging.getLogger("shots")
    if not hasattr(args, "video_profile"):
        args.video_profile = None
    if not hasattr(args, "profile_only"):
        args.profile_only = False
    if not hasattr(args, "scene_threshold"):
        args.scene_threshold = DEFAULT_SCENE_THRESHOLD
    if not hasattr(args, "scene_scale_width"):
        args.scene_scale_width = DEFAULT_SCENE_SCALE_WIDTH
    if not hasattr(args, "scene_min_gap"):
        args.scene_min_gap = DEFAULT_SCENE_MIN_GAP
    if not hasattr(args, "max_shot_len"):
        args.max_shot_len = 0.0
    if not hasattr(args, "frame_sampling"):
        args.frame_sampling = DEFAULT_FRAME_SAMPLING
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
    if args.frame_sampling not in {"per-shot", "batch"}:
        raise ShotsError("--frame-sampling must be per-shot or batch")
    if not 0 <= args.min_brightness <= 1:
        raise ShotsError("--min-brightness must be between 0 and 1")
    if not 0 < args.scene_threshold < 1:
        raise ShotsError("--scene-threshold must be between 0 and 1")
    if args.scene_scale_width < 0:
        raise ShotsError("--scene-scale-width must be >= 0")
    if args.scene_min_gap < 0:
        raise ShotsError("--scene-min-gap must be >= 0")
    if args.max_shot_len < 0:
        raise ShotsError("--max-shot-len must be >= 0")
    require_ffmpeg()
    profile = load_video_profile(args.video_profile)
    profile_hash = video_profile_hash(args.video_profile)
    cache = ShotsCache(work_dir, force=args.force)
    cache.prepare()

    logger.info("[1/4] Detecting shots")
    detect_key = detection_cache_key(input_path, args)
    cached_detection = cache.read_cached("detection.json", detect_key)
    if cached_detection is None:
        cached_detection = cache.read_cached("detection.json", legacy_detection_cache_key(input_path, args))
    if cached_detection is not None:
        spans = spans_from_json(cached_detection["spans"])
        duration_s = float(cached_detection["duration_s"])
    else:
        if args.profile_only:
            raise ShotsError("profile-only requires existing features cache")
        spans, duration_s = detect_shots(
            input_path,
            detector=args.detector,
            skip_intro=args.skip_intro,
            skip_outro=args.skip_outro,
            downscale=args.downscale,
            scene_threshold=args.scene_threshold,
            scene_scale_width=args.scene_scale_width,
            scene_min_gap=args.scene_min_gap,
            max_shot_len=args.max_shot_len,
        )
        cache.write_cached("detection.json", detect_key, {"duration_s": duration_s, "spans": spans_to_json(spans)})

    logger.info("[2/4] Computing features")
    warnings: list[str] = []
    face_detector, face_warnings = create_face_detector(args.face_detection)
    warnings.extend(face_warnings)
    face_detector_version = "off" if args.face_detection == "off" or face_warnings else "opencv-haar-frontalface-default"
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
        if args.profile_only:
            raise ShotsError("profile-only requires existing features cache")
        features_by_index = compute_features_per_shot(
            input_path=input_path,
            thumb_dir=thumb_dir,
            spans=spans,
            args=args,
            config=config,
            face_detector=face_detector,
            logger=logger,
        )
        cache.write_cached("features.json", feature_key, {str(key): value for key, value in features_by_index.items()})

    logger.info("[3/4] Applying video profile")
    profile_key = profile_cache_key(feature_key, profile_hash)
    cached_profile = cache.read_cached("profile_marking.json", profile_key)
    if cached_profile is not None:
        shots = [Shot.model_validate(item) for item in cached_profile["shots"]]
        n_non_story = int(cached_profile["n_non_story"])
    else:
        base_shots = features_to_shots(input_path, output_path, thumb_dir, spans, features_by_index)
        shots, n_non_story = apply_video_profile_to_shots(base_shots, profile)
        cache.write_cached("profile_marking.json", profile_key, {"n_non_story": n_non_story, "shots": [shot.model_dump(mode="json") for shot in shots]})
    shots = validate_shots(clamp_shots_to_duration(shots, duration_s), duration_s)

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
            "frame_sampling": args.frame_sampling,
            "face_detection": args.face_detection,
            "min_brightness": args.min_brightness,
            "min_shot_len": args.min_shot_len,
            "skip_intro": args.skip_intro,
            "skip_outro": args.skip_outro,
            "downscale": args.downscale,
            "scene_threshold": args.scene_threshold,
            "scene_scale_width": args.scene_scale_width,
            "scene_min_gap": args.scene_min_gap,
            "max_shot_len": args.max_shot_len,
        },
        model_versions={"face_detector": face_detector_version},
        video_profile_path=str(args.video_profile.expanduser().resolve()) if args.video_profile else None,
        video_profile_hash=profile_hash,
        n_non_story=n_non_story,
        intro_detection=profile.intro.model_dump(mode="json") if profile else None,
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
