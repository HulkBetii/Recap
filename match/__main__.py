from __future__ import annotations

import argparse
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from common.schema import BeatTiming, EdlMeta, EdlPlacement, ReviewBeat, Shot, validate_edl, write_json
from match.cache import MatchCache, file_hash, stable_hash
from match.fill import assign_timeline, fill_beat
from match.inputs import load_beats_timing, load_review_script, load_shots
from match.scoring import ScoringWeights
from match.timing import average_clip_len, validate_timeline


class MatchError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 5 match: review + timing + shots -> edl.json")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--shots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-clip", default=3.0, type=float)
    parser.add_argument("--max-clip", default=5.0, type=float)
    parser.add_argument("--widen-margin", default=15.0, type=float)
    parser.add_argument("--max-widen", default=3, type=int)
    parser.add_argument("--allow-repeat", action="store_true", default=True)
    parser.add_argument("--no-allow-repeat", dest="allow_repeat", action="store_false")
    parser.add_argument("--allow-speedfit", action="store_true", default=False)
    parser.add_argument("--no-allow-speedfit", dest="allow_speedfit", action="store_false")
    parser.add_argument("--seed", default=1234, type=int)
    parser.add_argument("--work-dir", default=Path("work/match"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--w-motion", default=0.60, type=float)
    parser.add_argument("--w-face", default=0.18, type=float)
    parser.add_argument("--w-bright", default=0.12, type=float)
    parser.add_argument("--w-reuse", default=0.35, type=float)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def make_cache_key(args: argparse.Namespace) -> str:
    return stable_hash({
        "review_script": file_hash(args.review_script.expanduser().resolve()),
        "beats_timing": file_hash(args.beats_timing.expanduser().resolve()),
        "shots": file_hash(args.shots.expanduser().resolve()),
        "min_clip": args.min_clip,
        "max_clip": args.max_clip,
        "widen_margin": args.widen_margin,
        "max_widen": args.max_widen,
        "allow_repeat": args.allow_repeat,
        "allow_speedfit": args.allow_speedfit,
        "seed": args.seed,
        "weights": [args.w_motion, args.w_face, args.w_bright, args.w_reuse],
    })


def validate_args(args: argparse.Namespace) -> None:
    for path_name in ("review_script", "beats_timing", "shots"):
        path = getattr(args, path_name).expanduser().resolve()
        if not path.is_file():
            raise MatchError(f"input file does not exist: {path}")
    if args.min_clip <= 0 or args.max_clip <= 0:
        raise MatchError("--min-clip and --max-clip must be > 0")
    if args.max_clip < args.min_clip:
        raise MatchError("--max-clip must be >= --min-clip")
    if args.widen_margin < 0 or args.max_widen < 0:
        raise MatchError("widen settings must be >= 0")


def run_match(args: argparse.Namespace) -> int:
    logger = logging.getLogger("match")
    validate_args(args)
    random.seed(args.seed)
    output_path = args.output.expanduser().resolve()
    cache = MatchCache(args.work_dir.expanduser().resolve(), force=args.force)
    cache.prepare()
    cache_key = make_cache_key(args)
    cached = cache.read_plan(cache_key)
    if cached is not None:
        edl = [EdlPlacement.model_validate(item) for item in cached["edl"]]
        meta = EdlMeta.model_validate(cached["meta"])
        meta = meta.model_copy(update={"cache_hits": cache.cache_hits})
        write_json(output_path, edl)
        write_json(output_path.with_name("edl.meta.json"), meta)
        return 0

    review_beats = load_review_script(args.review_script.expanduser().resolve())
    timings = load_beats_timing(args.beats_timing.expanduser().resolve())
    shots = load_shots(args.shots.expanduser().resolve())
    beats_by_id = {beat.beat_id: beat for beat in review_beats}
    timings_by_id = {timing.beat_id: timing for timing in timings}
    missing = sorted(set(beats_by_id) ^ set(timings_by_id))
    if missing:
        raise MatchError(f"review_script and beats_timing beat ids differ: {missing}")
    weights = ScoringWeights(args.w_motion, args.w_face, args.w_bright, args.w_reuse)
    reuse_counts: dict[int, int] = {}
    placements: list[EdlPlacement] = []
    warnings: list[str] = []
    n_beats_widened = 0
    n_reused = 0
    n_speedfit = 0

    logger.info("Matching %d beats", len(timings))
    for timing in sorted(timings, key=lambda item: item.tl_start):
        beat = beats_by_id[timing.beat_id]
        result = fill_beat(
            beat=beat,
            timing=timing,
            shots=shots,
            reuse_counts=reuse_counts,
            weights=weights,
            min_clip=args.min_clip,
            max_clip=args.max_clip,
            widen_margin=args.widen_margin,
            max_widen=args.max_widen,
            allow_repeat=args.allow_repeat,
            allow_speedfit=args.allow_speedfit,
        )
        if result.widened:
            n_beats_widened += 1
        n_reused += result.reused_count
        n_speedfit += result.speedfit_count
        warnings.extend(result.warnings)
        placements.extend(assign_timeline(result.fragments, timing))

    total_duration = max((timing.tl_end for timing in timings), default=0.0)
    placements = validate_timeline(placements, total_duration)
    coverage_ok = len(warnings) == 0
    meta = EdlMeta(
        total_duration_s=total_duration,
        n_placements=len(placements),
        n_beats_widened=n_beats_widened,
        n_reused=n_reused,
        n_speedfit=n_speedfit,
        avg_clip_len=round(average_clip_len(placements), 3),
        coverage_ok=coverage_ok,
        warnings=warnings,
        seed=args.seed,
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
    )
    write_json(output_path, placements)
    write_json(output_path.with_name("edl.meta.json"), meta)
    cache.write_plan(cache_key, [item.model_dump() for item in placements], meta.model_dump(mode="json"))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_match(args)
    except (MatchError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"match: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
