from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

from common.media import require_ffmpeg
from common.schema import write_json
from preflight.detect import PreflightError, build_video_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 0 video profile: detect intro/non-story ranges")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-intro-s", default=240.0, type=float)
    parser.add_argument("--sample-every-s", default=5.0, type=float)
    parser.add_argument("--classifier", default="heuristic", choices=["heuristic", "openclip"])
    parser.add_argument("--confidence-threshold", default=0.75, type=float)
    parser.add_argument("--uncertain-threshold", default=0.55, type=float)
    parser.add_argument("--work-dir", default=Path("work/preflight"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser

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
    require_ffmpeg()
    if args.force and work_dir.exists():
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
    write_json(output_path, profile)
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
