from __future__ import annotations

import argparse
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from common.schema import BeatTiming, EdlMeta, EdlPlacement, ReviewBeat, Shot, validate_edl, write_json
from match.cache import MatchCache, file_hash, stable_hash
from match.fill import assign_timeline, fill_beat, fill_timeline_gaps
from match.inputs import load_beats_timing, load_film_map, load_review_script, load_shots
from match.qa import build_edl_qa
from match.review_html import write_review_html
from match.scoring import ScoringWeights
from match.semantic import DEFAULT_EMBEDDING_MODEL, SemanticConfig, SemanticError, compute_semantic_result
from match.timing import average_clip_len, validate_timeline


class MatchError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 5 match: review + timing + shots -> edl.json")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--shots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--output-review-html", default=None, type=Path)
    parser.add_argument("--review-asset-dir", default=None, type=Path)
    parser.add_argument("--review-thumbs-per-beat", default=8, type=int)
    parser.add_argument("--review-html", action="store_true", default=True)
    parser.add_argument("--no-review-html", dest="review_html", action="store_false")
    parser.add_argument("--film-map", default=None, type=Path)
    parser.add_argument("--semantic-mode", default="off", choices=["off", "tfidf", "bge-m3"])
    parser.add_argument("--semantic-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--semantic-device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--semantic-batch-size", default=16, type=int)
    parser.add_argument("--semantic-cache-dir", default=None, type=Path)
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
    parser.add_argument("--w-semantic", default=0.35, type=float)
    parser.add_argument("--min-semantic-score", default=0.12, type=float)
    parser.add_argument("--exclude-non-story", action="store_true", default=True)
    parser.add_argument("--no-exclude-non-story", dest="exclude_non_story", action="store_false")
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
        "weights": [args.w_motion, args.w_face, args.w_bright, args.w_reuse, args.w_semantic],
        "film_map": file_hash(args.film_map.expanduser().resolve()) if args.film_map else None,
        "semantic_mode": args.semantic_mode,
        "semantic_model": args.semantic_model,
        "semantic_device": args.semantic_device,
        "semantic_batch_size": args.semantic_batch_size,
        "semantic_cache_dir": str(args.semantic_cache_dir) if args.semantic_cache_dir else None,
        "min_semantic_score": args.min_semantic_score,
        "exclude_non_story": args.exclude_non_story,
        "review_html": args.review_html,
        "review_thumbs_per_beat": args.review_thumbs_per_beat,
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
    if args.semantic_mode != "off" and args.film_map is None:
        raise MatchError("--film-map is required when --semantic-mode=tfidf")
    if args.film_map is not None and not args.film_map.expanduser().resolve().is_file():
        raise MatchError(f"input file does not exist: {args.film_map}")
    if args.min_semantic_score < 0:
        raise MatchError("--min-semantic-score must be >= 0")
    if args.semantic_batch_size <= 0:
        raise MatchError("--semantic-batch-size must be > 0")
    if args.review_thumbs_per_beat < 0:
        raise MatchError("--review-thumbs-per-beat must be >= 0")


def review_html_paths(args: argparse.Namespace, output_path: Path) -> tuple[Path, Path]:
    html_path = args.output_review_html.expanduser().resolve() if args.output_review_html else output_path.with_name("edl.review.html")
    asset_dir = args.review_asset_dir.expanduser().resolve() if args.review_asset_dir else output_path.with_name("edl.review")
    return html_path, asset_dir


def maybe_write_review_html(args: argparse.Namespace, output_path: Path, qa: dict) -> None:
    if not args.review_html:
        return
    html_path, asset_dir = review_html_paths(args, output_path)
    write_review_html(
        output_path=html_path,
        asset_dir=asset_dir,
        shots_path=args.shots.expanduser().resolve(),
        beats=load_review_script(args.review_script.expanduser().resolve()),
        placements=[EdlPlacement.model_validate(item) for item in json.loads(output_path.read_text(encoding="utf-8"))] if output_path.is_file() else [],
        shots=load_shots(args.shots.expanduser().resolve()),
        qa=qa,
        thumbs_per_beat=args.review_thumbs_per_beat,
    )


def run_match(args: argparse.Namespace) -> int:
    logger = logging.getLogger("match")
    if not hasattr(args, "exclude_non_story"):
        args.exclude_non_story = True
    if not hasattr(args, "review_html"):
        args.review_html = True
    if not hasattr(args, "output_review_html"):
        args.output_review_html = None
    if not hasattr(args, "review_asset_dir"):
        args.review_asset_dir = None
    if not hasattr(args, "review_thumbs_per_beat"):
        args.review_thumbs_per_beat = 8
    validate_args(args)
    random.seed(args.seed)
    output_path = args.output.expanduser().resolve()
    qa_path = args.output_qa.expanduser().resolve() if args.output_qa else output_path.with_name("edl.qa.json")
    review_html_path, _review_asset_dir = review_html_paths(args, output_path)
    cache = MatchCache(args.work_dir.expanduser().resolve(), force=args.force)
    cache.prepare()
    cache_key = make_cache_key(args)
    cached = cache.read_plan(cache_key)
    if cached is not None and "qa" not in cached:
        cached = None
    if cached is not None:
        edl = [EdlPlacement.model_validate(item) for item in cached["edl"]]
        meta = EdlMeta.model_validate(cached["meta"])
        meta = meta.model_copy(update={"cache_hits": cache.cache_hits})
        write_json(output_path, edl)
        write_json(output_path.with_name("edl.meta.json"), meta)
        if "qa" in cached:
            write_json(qa_path, cached["qa"])
            maybe_write_review_html(args, output_path, cached["qa"])
        return 0

    review_beats = load_review_script(args.review_script.expanduser().resolve())
    timings = load_beats_timing(args.beats_timing.expanduser().resolve())
    all_shots = load_shots(args.shots.expanduser().resolve())
    n_intro_excluded = sum(1 for shot in all_shots if not shot.is_story)
    shots = [shot for shot in all_shots if shot.is_story] if args.exclude_non_story else all_shots
    beats_by_id = {beat.beat_id: beat for beat in review_beats}
    timings_by_id = {timing.beat_id: timing for timing in timings}
    missing = sorted(set(beats_by_id) ^ set(timings_by_id))
    if missing:
        raise MatchError(f"review_script and beats_timing beat ids differ: {missing}")
    semantic_result = None
    semantic_scores: dict[tuple[int, int], float] = {}
    if args.semantic_mode != "off":
        film_map = load_film_map(args.film_map.expanduser().resolve())
        semantic_cache_dir = args.semantic_cache_dir.expanduser().resolve() if args.semantic_cache_dir else args.work_dir.expanduser().resolve() / "semantic"
        semantic_result = compute_semantic_result(
            review_beats,
            shots,
            film_map,
            SemanticConfig(
                mode=args.semantic_mode,
                model=args.semantic_model,
                device=args.semantic_device,
                batch_size=args.semantic_batch_size,
                cache_dir=semantic_cache_dir,
            ),
        )
        semantic_scores = semantic_result.scores
    weights = ScoringWeights(args.w_motion, args.w_face, args.w_bright, args.w_reuse, args.w_semantic)
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
            semantic_scores=semantic_scores,
        )
        if result.widened:
            n_beats_widened += 1
        n_reused += result.reused_count
        n_speedfit += result.speedfit_count
        warnings.extend(result.warnings)
        placements.extend(assign_timeline(result.fragments, timing))

    total_duration = max((timing.tl_end for timing in timings), default=0.0)
    before_gap_fill = len(placements)
    placements = fill_timeline_gaps(placements, total_duration)
    pause_fillers = len(placements) - before_gap_fill
    if pause_fillers:
        warnings.append(f"inserted {pause_fillers} pause filler placement(s) to cover TTS inter-beat silence")
        n_reused += pause_fillers
    placements = validate_timeline(placements, total_duration)
    coverage_ok = not any("pause filler" not in warning for warning in warnings)
    meta = EdlMeta(
        total_duration_s=total_duration,
        n_placements=len(placements),
        n_beats_widened=n_beats_widened,
        n_reused=n_reused,
        n_speedfit=n_speedfit,
        n_intro_excluded=n_intro_excluded if args.exclude_non_story else 0,
        avg_clip_len=round(average_clip_len(placements), 3),
        coverage_ok=coverage_ok,
        warnings=warnings,
        seed=args.seed,
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
    )
    qa = build_edl_qa(
        beats=review_beats,
        placements=placements,
        shots=all_shots,
        semantic_scores=semantic_scores,
        weights=weights,
        semantic_result=semantic_result,
        min_semantic_score=args.min_semantic_score,
        warnings=warnings,
    )
    write_json(output_path, placements)
    write_json(output_path.with_name("edl.meta.json"), meta)
    write_json(qa_path, qa)
    maybe_write_review_html(args, output_path, qa)
    cache.write_plan(cache_key, [item.model_dump(mode="json") for item in placements], meta.model_dump(mode="json"), qa)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_match(args)
    except (MatchError, SemanticError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"match: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
