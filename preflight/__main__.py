from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

from common.inputs import load_anime_context, load_manual_non_story_ranges
from common.media import require_ffmpeg
from common.integrity import atomic_write_json, file_hash
from common.schema import NonStoryRange, write_json
from preflight.detect import PreflightError, build_video_profile
from preflight.integrity import PREFLIGHT_CACHE_VERSION, preflight_identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 0 video profile: detect intro/non-story ranges")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-intro-s", default=240.0, type=float)
    parser.add_argument("--sample-every-s", default=5.0, type=float)
    parser.add_argument("--classifier", default="heuristic", choices=["heuristic", "openclip"])
    parser.add_argument("--confidence-threshold", default=0.75, type=float)
    parser.add_argument("--uncertain-threshold", default=0.55, type=float)
    parser.add_argument("--manual-ranges", default=None, type=Path, help="Optional YAML/JSON file containing strict manual non-story ranges")
    parser.add_argument("--anime-context", default=None, type=Path, help="Optional anime context YAML/JSON; its non_story_ranges are merged")
    parser.add_argument("--work-dir", default=Path("work/preflight"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser

def resolve_optional_file(path: Path | None, *, label: str) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PreflightError(f"{label} file does not exist: {resolved}")
    return resolved

def load_manual_ranges(manual_ranges_path: Path | None, anime_context_path: Path | None) -> list[NonStoryRange]:
    ranges: list[NonStoryRange] = []
    if manual_ranges_path is not None:
        ranges.extend(load_manual_non_story_ranges(manual_ranges_path))
    if anime_context_path is not None:
        context = load_anime_context(anime_context_path)
        ranges.extend(NonStoryRange.model_validate(item.model_dump(mode="json")) for item in context.non_story_ranges)
    return ranges

def merge_manual_ranges(profile, ranges: list[NonStoryRange]):  # type: ignore[no-untyped-def]
    if not ranges:
        return profile
    existing = list(profile.non_story_ranges)
    appended: list[NonStoryRange] = []
    seen = {(round(item.start_s, 3), round(item.end_s, 3), item.label) for item in existing}
    for item in ranges:
        if item.end_s > profile.duration_s + 1e-6:
            raise PreflightError(f"manual non-story range exceeds media duration: {item.label} {item.start_s}-{item.end_s}s")
        if item.start_s >= profile.duration_s:
            raise PreflightError(f"manual non-story range starts after media duration: {item.label} {item.start_s}s")
        key = (round(item.start_s, 3), round(item.end_s, 3), item.label)
        if key in seen:
            continue
        seen.add(key)
        appended.append(item)
    if not appended:
        return profile
    warnings = list(profile.warnings)
    warnings.append(f"merged {len(appended)} manual non-story range(s)")
    merged_ranges = sorted(existing + appended, key=lambda item: (item.start_s, item.end_s, item.label))
    return profile.model_copy(update={"non_story_ranges": merged_ranges, "warnings": warnings})

def run_preflight(args: argparse.Namespace) -> int:
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    if not input_path.is_file():
        raise PreflightError(f"input video does not exist: {input_path}")
    if args.max_intro_s <= 0:
        raise PreflightError("--max-intro-s must be > 0")
    if args.sample_every_s <= 0:
        raise PreflightError("--sample-every-s must be > 0")
    if not 0 <= args.uncertain_threshold <= args.confidence_threshold <= 1:
        raise PreflightError("thresholds must satisfy 0 <= uncertain <= confidence <= 1")
    if not hasattr(args, "manual_ranges"):
        args.manual_ranges = None
    if not hasattr(args, "anime_context"):
        args.anime_context = None
    manual_ranges_path = resolve_optional_file(args.manual_ranges, label="manual ranges")
    anime_context_path = resolve_optional_file(args.anime_context, label="anime context")
    require_ffmpeg()
    input_hash, config_hash = preflight_identity(
        input_path,
        classifier=args.classifier,
        max_intro_s=args.max_intro_s,
        sample_every_s=args.sample_every_s,
        confidence_threshold=args.confidence_threshold,
        uncertain_threshold=args.uncertain_threshold,
        manual_ranges_hash=file_hash(manual_ranges_path),
        anime_context_hash=file_hash(anime_context_path),
    )
    identity_path = work_dir / "cache_identity.json"
    cache_current = False
    if identity_path.is_file() and not args.force:
        try:
            cache_current = json.loads(identity_path.read_text(encoding="utf-8")) == {
                "cache_version": PREFLIGHT_CACHE_VERSION,
                "input_hash": input_hash,
                "config_hash": config_hash,
            }
        except (OSError, json.JSONDecodeError):
            cache_current = False
    if (args.force or not cache_current) and work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile = build_video_profile(
        input_path,
        work_dir,
        classifier=args.classifier,
        max_intro_s=args.max_intro_s,
        sample_every_s=args.sample_every_s,
        confidence_threshold=args.confidence_threshold,
        uncertain_threshold=args.uncertain_threshold,
    )
    profile = merge_manual_ranges(profile, load_manual_ranges(manual_ranges_path, anime_context_path))
    profile = profile.model_copy(
        update={
            "input_hash": input_hash,
            "config_hash": config_hash,
            "cache_version": PREFLIGHT_CACHE_VERSION,
        }
    )
    write_json(output_path, profile)
    atomic_write_json(identity_path, {
        "cache_version": PREFLIGHT_CACHE_VERSION,
        "input_hash": input_hash,
        "config_hash": config_hash,
    })
    logging.getLogger("preflight").info("Done: %s", output_path)
    return 0

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_preflight(args)
    except (PreflightError, OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"preflight: error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
