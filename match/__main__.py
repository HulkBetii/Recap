from __future__ import annotations

import argparse
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from common.schema import BeatTiming, EdlMeta, EdlPlacement, FilmMapSegment, ReviewBeat, ReviewIntent, Shot, StorySection, validate_edl, validate_review_intents, validate_story_map, write_json
from match.anchors import plan_content_anchors
from match.cache import MatchCache, file_hash, stable_hash
from match.fill import assign_timeline, chronology_tier, fill_beat, fill_timeline_gaps, split_long_placements
from match.inputs import load_beats_timing, load_film_map, load_review_script, load_shots
from match.intra_beat import alignment_queries, apply_hook_leading_brightness_guard, apply_intra_beat_alignment, long_beat_alignment_required, prepare_intra_beat_alignment_sentences, update_reuse_counts
from match.qa import build_edl_qa
from match.review_html import write_review_html
from match.scoring import ScoringWeights, score_shot
from match.semantic import DEFAULT_EMBEDDING_MODEL, SemanticConfig, SemanticError, compute_semantic_result
from match.sync_qa import build_sync_qa
from match.timing import average_clip_len, validate_source_bounds, validate_timeline
from match.visual import VisualMatchError, build_visual_qa, compute_visual_scores
from match.version import MATCH_ALGORITHM_VERSION
from visual_index.encoder import VisualEncoderError
from visual_index.integrity import visual_index_artifact_hash


class MatchError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 5 match: review + timing + shots -> edl.json")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--shots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--output-sync-qa", default=None, type=Path)
    parser.add_argument("--output-review-html", default=None, type=Path)
    parser.add_argument("--review-asset-dir", default=None, type=Path)
    parser.add_argument("--review-thumbs-per-beat", default=8, type=int)
    parser.add_argument("--review-html", action="store_true", default=True)
    parser.add_argument("--no-review-html", dest="review_html", action="store_false")
    parser.add_argument("--film-map", default=None, type=Path)
    parser.add_argument("--review-intent", default=None, type=Path)
    parser.add_argument("--story-map", default=None, type=Path)
    parser.add_argument("--visual-index", default=None, type=Path)
    parser.add_argument("--visual-mode", default="off", choices=["off", "rerank"])
    parser.add_argument("--visual-cache-dir", default=None, type=Path)
    parser.add_argument("--visual-device", default=None, choices=["auto", "cpu", "cuda"])
    parser.add_argument("--visual-batch-size", default=None, type=int)
    parser.add_argument("--output-visual-qa", default=None, type=Path)
    parser.add_argument("--w-visual", default=0.20, type=float)
    parser.add_argument("--semantic-mode", default="off", choices=["off", "tfidf", "bge-m3"])
    parser.add_argument("--semantic-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--semantic-device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--semantic-batch-size", default=16, type=int)
    parser.add_argument("--semantic-cache-dir", default=None, type=Path)
    parser.add_argument("--content-anchors", action="store_true", default=True)
    parser.add_argument("--no-content-anchors", dest="content_anchors", action="store_false")
    parser.add_argument("--min-clip", default=3.0, type=float)
    parser.add_argument("--max-clip", default=5.0, type=float)
    parser.add_argument("--min-visual-clip", default=0.6, type=float)
    parser.add_argument("--widen-margin", default=15.0, type=float)
    parser.add_argument("--max-widen", default=3, type=int)
    parser.add_argument("--allow-dark-fallback", action="store_true", default=True)
    parser.add_argument("--no-allow-dark-fallback", dest="allow_dark_fallback", action="store_false")
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
    parser.add_argument("--match-strategy", default="hybrid", choices=["chronological", "hybrid", "semantic"])
    parser.add_argument("--chronology-weight", default=0.70, type=float)
    parser.add_argument("--max-source-drift-s", default=12.0, type=float)
    parser.add_argument("--exclude-non-story", action="store_true", default=True)
    parser.add_argument("--exclude-end-credits", action="store_true", default=False)
    parser.add_argument("--no-exclude-end-credits", dest="exclude_end_credits", action="store_false")
    parser.add_argument("--max-repeat-per-beat", default=2, type=int)
    parser.add_argument("--max-repeat-ratio-per-beat", default=0.35, type=float)
    parser.add_argument("--min-repeat-alternative-score-ratio", default=0.75, type=float)
    parser.add_argument("--adjacent-shot-repeat-penalty", default=0.50, type=float)
    parser.add_argument("--opening-guard-s", default=0.0, type=float)
    parser.add_argument("--opening-max-repeat-ratio", default=0.20, type=float)
    parser.add_argument("--opening-max-repeat-per-shot", default=1, type=int)
    parser.add_argument("--opening-min-unique-shots", default=4, type=int)
    parser.add_argument("--opening-story-visual-start", action="store_true", default=True)
    parser.add_argument("--no-opening-story-visual-start", dest="opening_story_visual_start", action="store_false")
    parser.add_argument("--opening-allow-short-fill", action="store_true", default=True)
    parser.add_argument("--no-opening-allow-short-fill", dest="opening_allow_short_fill", action="store_false")
    parser.add_argument("--opening-ordered-fill", action="store_true", default=True)
    parser.add_argument("--no-opening-ordered-fill", dest="opening_ordered_fill", action="store_false")
    parser.add_argument("--opening-intra-beat-align", action="store_true", default=False)
    parser.add_argument("--no-opening-intra-beat-align", dest="opening_intra_beat_align", action="store_false")
    parser.add_argument("--hook-min-brightness", default=0.0, type=float)
    parser.add_argument("--ordered-fill-by-audio-progress", action="store_true", default=True)
    parser.add_argument("--no-ordered-fill-by-audio-progress", dest="ordered_fill_by_audio_progress", action="store_false")
    parser.add_argument("--no-exclude-non-story", dest="exclude_non_story", action="store_false")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def make_cache_key(args: argparse.Namespace) -> str:
    film_map_path = args.film_map.expanduser().resolve() if args.film_map else None
    film_map_meta = film_map_path.with_name("film_map.meta.json") if film_map_path else None
    return stable_hash({
        "algorithm_version": MATCH_ALGORITHM_VERSION,
        "review_script": file_hash(args.review_script.expanduser().resolve()),
        "beats_timing": file_hash(args.beats_timing.expanduser().resolve()),
        "shots": file_hash(args.shots.expanduser().resolve()),
        "min_clip": args.min_clip,
        "max_clip": args.max_clip,
        "min_visual_clip": args.min_visual_clip,
        "widen_margin": args.widen_margin,
        "max_widen": args.max_widen,
        "allow_dark_fallback": args.allow_dark_fallback,
        "allow_repeat": args.allow_repeat,
        "allow_speedfit": args.allow_speedfit,
        "seed": args.seed,
        "weights": [args.w_motion, args.w_face, args.w_bright, args.w_reuse, args.w_semantic],
        "film_map": file_hash(film_map_path) if film_map_path else None,
        "film_map_meta": file_hash(film_map_meta) if film_map_meta and film_map_meta.is_file() else None,
        "review_intent": file_hash(args.review_intent.expanduser().resolve()) if args.review_intent else None,
        "story_map": file_hash(args.story_map.expanduser().resolve()) if args.story_map else None,
        "semantic_mode": args.semantic_mode,
        "semantic_model": args.semantic_model,
        "semantic_device": args.semantic_device,
        "semantic_batch_size": args.semantic_batch_size,
        "semantic_cache_dir": str(args.semantic_cache_dir) if args.semantic_cache_dir else None,
        "content_anchors": args.content_anchors,
        "opening_intra_beat_align": args.opening_intra_beat_align,
        "hook_min_brightness": args.hook_min_brightness,
        "visual_index": visual_index_artifact_hash(args.visual_index.expanduser().resolve()) if args.visual_index and args.visual_index.expanduser().resolve().is_file() else None,
        "visual_mode": args.visual_mode,
        "visual_cache_dir": str(args.visual_cache_dir) if args.visual_cache_dir else None,
        "visual_device": args.visual_device,
        "visual_batch_size": args.visual_batch_size,
        "w_visual": args.w_visual,
        "min_semantic_score": args.min_semantic_score,
        "match_strategy": args.match_strategy,
        "chronology_weight": args.chronology_weight,
        "max_source_drift_s": args.max_source_drift_s,
        "exclude_non_story": args.exclude_non_story,
        "exclude_end_credits": args.exclude_end_credits,
        "review_html": args.review_html,
        "sync_qa": True,
        "review_thumbs_per_beat": args.review_thumbs_per_beat,
        "repeat_guard": [args.max_repeat_per_beat, args.max_repeat_ratio_per_beat, args.min_repeat_alternative_score_ratio, args.adjacent_shot_repeat_penalty],
        "opening_guard": [args.opening_guard_s, args.opening_max_repeat_ratio, args.opening_max_repeat_per_shot, args.opening_min_unique_shots, args.opening_allow_short_fill, args.opening_ordered_fill, args.ordered_fill_by_audio_progress, args.opening_story_visual_start, args.hook_min_brightness],
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
    if args.min_visual_clip < 0:
        raise MatchError("--min-visual-clip must be >= 0")
    if args.widen_margin < 0 or args.max_widen < 0:
        raise MatchError("widen settings must be >= 0")
    if args.semantic_mode != "off" and args.film_map is None:
        raise MatchError("--film-map is required when --semantic-mode=tfidf")
    if args.film_map is not None and not args.film_map.expanduser().resolve().is_file():
        raise MatchError(f"input file does not exist: {args.film_map}")
    if args.review_intent is not None and not args.review_intent.expanduser().resolve().is_file():
        raise MatchError(f"input file does not exist: {args.review_intent}")
    if args.story_map is not None and not args.story_map.expanduser().resolve().is_file():
        raise MatchError(f"input file does not exist: {args.story_map}")
    if args.visual_index is not None and not args.visual_index.expanduser().resolve().is_file():
        logging.getLogger("match").warning("visual index does not exist; falling back to text-only matching: %s", args.visual_index)
    if args.min_semantic_score < 0:
        raise MatchError("--min-semantic-score must be >= 0")
    if args.w_visual < 0:
        raise MatchError("--w-visual must be >= 0")
    if args.visual_batch_size is not None and args.visual_batch_size <= 0:
        raise MatchError("--visual-batch-size must be > 0")
    if args.chronology_weight < 0:
        raise MatchError("--chronology-weight must be >= 0")
    if args.max_source_drift_s <= 0:
        raise MatchError("--max-source-drift-s must be > 0")
    if not 0 <= args.hook_min_brightness <= 1:
        raise MatchError("--hook-min-brightness must be between 0 and 1")
    if args.semantic_batch_size <= 0:
        raise MatchError("--semantic-batch-size must be > 0")
    if args.review_thumbs_per_beat < 0:
        raise MatchError("--review-thumbs-per-beat must be >= 0")
    if args.max_repeat_per_beat < 0:
        raise MatchError("--max-repeat-per-beat must be >= 0")
    if args.max_repeat_ratio_per_beat < 0:
        raise MatchError("--max-repeat-ratio-per-beat must be >= 0")
    if args.min_repeat_alternative_score_ratio < 0:
        raise MatchError("--min-repeat-alternative-score-ratio must be >= 0")
    if args.adjacent_shot_repeat_penalty < 0:
        raise MatchError("--adjacent-shot-repeat-penalty must be >= 0")
    if args.opening_guard_s < 0:
        raise MatchError("--opening-guard-s must be >= 0")
    if args.opening_max_repeat_ratio < 0:
        raise MatchError("--opening-max-repeat-ratio must be >= 0")
    if args.opening_max_repeat_per_shot < 0:
        raise MatchError("--opening-max-repeat-per-shot must be >= 0")
    if args.opening_min_unique_shots < 0:
        raise MatchError("--opening-min-unique-shots must be >= 0")


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



def load_review_intents(path: Path | None, beats: list[ReviewBeat]) -> dict[int, ReviewIntent]:
    if path is None:
        return {}
    raw = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    intents = [ReviewIntent.model_validate(item) for item in raw]
    validate_review_intents(intents, beats)
    return {intent.beat_id: intent for intent in intents}

def load_story_sections(path: Path | None) -> dict[int, StorySection]:
    if path is None:
        return {}
    raw = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    sections = validate_story_map([StorySection.model_validate(item) for item in raw])
    return {section.section_id: section for section in sections}


def opening_story_visual_start(beat: ReviewBeat, film_map: list[FilmMapSegment], *, max_start_s: float = 90.0) -> float | None:
    if beat.src_tc_start > 1.0:
        return None
    candidates = [
        segment
        for segment in film_map
        if segment.type == "visual"
        and segment.scene_desc
        and beat.src_tc_start <= segment.tc_start < min(beat.src_tc_end, max_start_s)
        and not is_opening_non_story_description(segment.scene_desc)
    ]
    if not candidates:
        return None
    first_visual = min(candidates, key=lambda segment: segment.tc_start)
    return round(first_visual.tc_start, 3)


def is_opening_non_story_description(text: str) -> bool:
    lowered = text.lower()
    markers = ("logo", "title card", "opening credits", "credits", "black screen", "white text", "production", "studio")
    return any(marker in lowered for marker in markers)


def content_anchors_allowed(film_map_path: Path | None) -> bool:
    if film_map_path is None:
        return False
    meta_path = film_map_path.expanduser().resolve().with_name("film_map.meta.json")
    if not meta_path.is_file():
        return True
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and not bool(raw.get("approximate_timecodes", False))

def run_match(args: argparse.Namespace) -> int:
    logger = logging.getLogger("match")
    if not hasattr(args, "exclude_non_story"):
        args.exclude_non_story = True
    if not hasattr(args, "exclude_end_credits"):
        args.exclude_end_credits = False
    for name, default in (("max_repeat_per_beat", 2), ("max_repeat_ratio_per_beat", 0.35), ("min_repeat_alternative_score_ratio", 0.75), ("adjacent_shot_repeat_penalty", 0.50), ("opening_guard_s", 0.0), ("opening_max_repeat_ratio", 0.20), ("opening_max_repeat_per_shot", 1), ("opening_min_unique_shots", 4), ("chronology_weight", 0.70), ("max_source_drift_s", 12.0)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not hasattr(args, "min_visual_clip"):
        args.min_visual_clip = 0.6
    if not hasattr(args, "allow_dark_fallback"):
        args.allow_dark_fallback = True
    if not hasattr(args, "opening_allow_short_fill"):
        args.opening_allow_short_fill = True
    if not hasattr(args, "opening_ordered_fill"):
        args.opening_ordered_fill = True
    if not hasattr(args, "opening_intra_beat_align"):
        args.opening_intra_beat_align = False
    if not hasattr(args, "hook_min_brightness"):
        args.hook_min_brightness = 0.0
    if not hasattr(args, "ordered_fill_by_audio_progress"):
        args.ordered_fill_by_audio_progress = True
    if not hasattr(args, "opening_story_visual_start"):
        args.opening_story_visual_start = True
    if not hasattr(args, "match_strategy"):
        args.match_strategy = "hybrid"
    if not hasattr(args, "review_html"):
        args.review_html = True
    if not hasattr(args, "output_review_html"):
        args.output_review_html = None
    if not hasattr(args, "review_asset_dir"):
        args.review_asset_dir = None
    if not hasattr(args, "review_thumbs_per_beat"):
        args.review_thumbs_per_beat = 8
    if not hasattr(args, "output_sync_qa"):
        args.output_sync_qa = None
    if not hasattr(args, "review_intent"):
        args.review_intent = None
    if not hasattr(args, "story_map"):
        args.story_map = None
    if not hasattr(args, "visual_index"):
        args.visual_index = None
    if not hasattr(args, "visual_mode"):
        args.visual_mode = "off"
    if not hasattr(args, "visual_cache_dir"):
        args.visual_cache_dir = None
    if not hasattr(args, "visual_device"):
        args.visual_device = None
    if not hasattr(args, "visual_batch_size"):
        args.visual_batch_size = None
    if not hasattr(args, "output_visual_qa"):
        args.output_visual_qa = None
    if not hasattr(args, "w_visual"):
        args.w_visual = 0.20
    if not hasattr(args, "content_anchors"):
        args.content_anchors = True
    validate_args(args)
    random.seed(args.seed)
    output_path = args.output.expanduser().resolve()
    qa_path = args.output_qa.expanduser().resolve() if args.output_qa else output_path.with_name("edl.qa.json")
    sync_qa_path = args.output_sync_qa.expanduser().resolve() if args.output_sync_qa else output_path.with_name("edl.sync.qa.json")
    visual_qa_path = args.output_visual_qa.expanduser().resolve() if args.output_visual_qa else output_path.with_name("edl.visual.qa.json")
    review_html_path, _review_asset_dir = review_html_paths(args, output_path)
    cache = MatchCache(args.work_dir.expanduser().resolve(), force=args.force)
    cache.prepare()
    cache_key = make_cache_key(args)
    cached = cache.read_plan(cache_key)
    if cached is not None and ("qa" not in cached or "sync_qa" not in cached or "visual_qa" not in cached):
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
        if "sync_qa" in cached:
            write_json(sync_qa_path, cached["sync_qa"])
        if "visual_qa" in cached:
            write_json(visual_qa_path, cached["visual_qa"])
        return 0

    review_beats = load_review_script(args.review_script.expanduser().resolve())
    review_intents = load_review_intents(args.review_intent, review_beats)
    story_sections = load_story_sections(args.story_map)
    timings = load_beats_timing(args.beats_timing.expanduser().resolve())
    all_shots = load_shots(args.shots.expanduser().resolve())
    n_intro_excluded = sum(1 for shot in all_shots if not shot.is_story)
    end_credit_shot_ids = {
        shot.index for shot in all_shots
        if args.exclude_end_credits and shot.is_end_credit
    }
    shots = [
        shot for shot in all_shots
        if (not args.exclude_non_story or shot.is_story)
        and shot.index not in end_credit_shot_ids
    ]
    beats_by_id = {beat.beat_id: beat for beat in review_beats}
    timings_by_id = {timing.beat_id: timing for timing in timings}
    missing = sorted(set(beats_by_id) ^ set(timings_by_id))
    if missing:
        raise MatchError(f"review_script and beats_timing beat ids differ: {missing}")
    film_map = load_film_map(args.film_map.expanduser().resolve()) if args.film_map else []
    semantic_result = None
    semantic_scores: dict[tuple[int, int], float] = {}
    visual_result = None
    visual_scores: dict[tuple[int, int], float] = {}
    warnings: list[str] = []
    strict_timecodes = content_anchors_allowed(args.film_map)
    use_content_anchors = args.content_anchors and strict_timecodes
    if args.content_anchors and args.film_map is not None and not use_content_anchors:
        warnings.append("content anchors disabled because film_map timecodes are not strict")
    intra_beat_alignment_sentences = prepare_intra_beat_alignment_sentences(
        beats=review_beats,
        timings=timings,
        enabled=args.opening_intra_beat_align,
        semantic_mode=args.semantic_mode,
        strict_timecodes=strict_timecodes,
        opening_guard_s=args.opening_guard_s,
    )
    if args.opening_intra_beat_align and not strict_timecodes:
        warnings.append("opening intra-beat alignment disabled because film_map timecodes are not strict")
    if args.semantic_mode != "off":
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
                score_segments=use_content_anchors,
            ),
            alignment_queries=alignment_queries(intra_beat_alignment_sentences),
        )
        semantic_scores = semantic_result.scores
    if args.visual_mode != "off" and args.visual_index is not None and args.visual_index.expanduser().resolve().is_file():
        try:
            visual_cache_dir = args.visual_cache_dir.expanduser().resolve() if args.visual_cache_dir else args.work_dir.expanduser().resolve() / "visual"
            visual_result = compute_visual_scores(
                beats=review_beats,
                shots=shots,
                review_intents=review_intents,
                index_path=args.visual_index.expanduser().resolve(),
                cache_dir=visual_cache_dir,
                device=args.visual_device or args.semantic_device,
                batch_size=args.visual_batch_size or args.semantic_batch_size,
            )
            visual_scores = visual_result.scores
            warnings.extend(f"visual: {warning}" for warning in visual_result.warnings)
        except (VisualMatchError, VisualEncoderError, ValueError, OSError) as exc:
            warnings.append(f"visual matching disabled: {exc}")
            logger.warning("Visual matching disabled: %s", exc)
    elif args.visual_mode != "off":
        warnings.append("visual matching requested but no usable visual index was provided")
    weights = ScoringWeights(args.w_motion, args.w_face, args.w_bright, args.w_reuse, args.w_semantic, args.w_visual if visual_scores else 0.0)
    reuse_counts: dict[int, int] = {}
    placements: list[EdlPlacement] = []
    n_beats_widened = 0
    n_reused = 0
    n_speedfit = 0
    candidate_shot_ids: dict[int, list[int]] = {}
    candidate_drift_tiers: dict[tuple[int, int], int] = {}
    candidate_diagnostics: dict[int, dict[str, object]] = {}
    n_dark_fallback_beats = 0
    n_capacity_exhausted_beats = 0
    n_unused_source_reuse = 0
    n_overlapping_repeats = 0

    logger.info("Matching %d beats", len(timings))
    for timing in sorted(timings, key=lambda item: item.tl_start):
        beat = beats_by_id[timing.beat_id]
        reuse_counts_before = dict(reuse_counts)
        in_opening_guard = args.opening_guard_s > 0 and timing.tl_start < args.opening_guard_s
        source_start_override = None
        if in_opening_guard and args.opening_story_visual_start and film_map:
            source_start_override = opening_story_visual_start(beat, film_map)
        anchor_plan = None
        if (
            use_content_anchors
            and not in_opening_guard
            and semantic_result is not None
            and semantic_result.segment_scores
            and film_map
        ):
            anchor_plan = plan_content_anchors(
                beat=beat,
                required_duration_s=timing.duration,
                shots=shots,
                film_map=film_map,
                segment_scores=semantic_result.segment_scores,
                max_clip=args.max_clip,
                min_visual_clip=args.min_visual_clip,
                allow_dark_fallback=args.allow_dark_fallback,
            )
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
            allow_repeat=args.allow_repeat and not (in_opening_guard and args.opening_allow_short_fill),
            allow_speedfit=args.allow_speedfit,
            semantic_scores=semantic_scores,
            visual_scores=visual_scores,
            max_repeat_per_beat=args.opening_max_repeat_per_shot if in_opening_guard else args.max_repeat_per_beat,
            max_repeat_ratio_per_beat=args.opening_max_repeat_ratio if in_opening_guard else args.max_repeat_ratio_per_beat,
            min_repeat_alternative_score_ratio=args.min_repeat_alternative_score_ratio,
            adjacent_shot_repeat_penalty=args.adjacent_shot_repeat_penalty,
            ordered_fill=(in_opening_guard and args.opening_ordered_fill) or args.match_strategy == "chronological",
            ordered_fill_by_audio_progress=args.ordered_fill_by_audio_progress or args.match_strategy == "chronological",
            match_strategy=args.match_strategy,
            chronology_weight=args.chronology_weight,
            max_source_drift_s=args.max_source_drift_s,
            source_start_override=source_start_override,
            min_visual_clip=args.min_visual_clip,
            strict_ordered_fill=in_opening_guard and args.opening_ordered_fill,
            allow_dark_fallback=args.allow_dark_fallback,
            candidate_filter_ids=anchor_plan.candidate_ids if anchor_plan else None,
            dark_candidate_ids=anchor_plan.dark_candidate_ids if anchor_plan else None,
            source_intervals=anchor_plan.intervals if anchor_plan else None,
        )
        beat_placements = assign_timeline(result.fragments, timing)
        intra_beat_result = None
        alignment_mode = None
        alignment_trigger_drift = None
        long_alignment_required = False
        if beat.beat_id in intra_beat_alignment_sentences and not in_opening_guard and anchor_plan is None:
            long_alignment_required, alignment_trigger_drift = long_beat_alignment_required(
                beat=beat,
                timing=timing,
                placements=beat_placements,
                max_source_drift_s=args.max_source_drift_s,
            )
        if (
            beat.beat_id in intra_beat_alignment_sentences
            and (in_opening_guard or long_alignment_required)
            and semantic_result is not None
            and semantic_result.provider == "bge-m3"
            and semantic_result.query_shot_scores
        ):
            alignment_mode = "opening" if in_opening_guard else "long_beat"
            intra_beat_result = apply_intra_beat_alignment(
                beat=beat,
                timing=timing,
                baseline_placements=beat_placements,
                sentences=intra_beat_alignment_sentences[beat.beat_id],
                shots=shots,
                query_shot_scores=semantic_result.query_shot_scores,
                reuse_counts_before=reuse_counts_before,
                max_clip=args.max_clip,
                min_visual_clip=args.min_visual_clip,
                allow_dark_fallback=args.allow_dark_fallback,
                mode=alignment_mode,
            )
            beat_placements = intra_beat_result.placements
            result.warnings.extend(intra_beat_result.warnings)
            if intra_beat_result.used:
                update_reuse_counts(reuse_counts, reuse_counts_before, beat_placements)

        hook_guard_result = apply_hook_leading_brightness_guard(
            beat=beat,
            baseline_placements=beat_placements,
            shots=shots,
            min_brightness=args.hook_min_brightness,
            max_clip=args.max_clip,
            min_visual_clip=args.min_visual_clip,
        )
        beat_placements = hook_guard_result.placements
        result.warnings.extend(hook_guard_result.warnings)
        if hook_guard_result.used:
            update_reuse_counts(reuse_counts, reuse_counts_before, beat_placements)

        selected_candidate_ids = list(result.candidate_shot_ids)
        if intra_beat_result is not None:
            selected_candidate_ids.extend(
                int(shot_index)
                for diagnostic in intra_beat_result.diagnostics
                for shot_index in diagnostic.get("selected_shot_ids", [])
            )
        selected_candidate_ids.extend(hook_guard_result.replacement_shot_ids)
        candidate_shot_ids[beat.beat_id] = list(dict.fromkeys(selected_candidate_ids))
        excluded_end_credit_ids = sorted(
            shot.index for shot in all_shots
            if shot.index in end_credit_shot_ids
            and shot.tc_start < result.window_end
            and result.window_start < shot.tc_end
        )
        if result.capacity_exhausted and excluded_end_credit_ids:
            result.warnings.append(
                f"beat {beat.beat_id} end-credit guard excluded {len(excluded_end_credit_ids)} shot(s) while footage was insufficient"
            )
        candidate_diagnostics[beat.beat_id] = {
            "window_start": result.window_start,
            "window_end": result.window_end,
            "widen_count": result.widen_count,
            "required_duration_s": result.required_duration_s,
            "primary_capacity_s": result.primary_capacity_s,
            "dark_capacity_s": result.dark_capacity_s,
            "total_capacity_s": result.total_capacity_s,
            "capacity_exhausted": result.capacity_exhausted,
            "dark_candidate_ids": result.dark_candidate_ids,
            "dark_selected_ids": result.dark_selected_ids,
            "unused_source_reuse_count": result.unused_source_reuse_count,
            "overlapping_repeat_count": result.overlapping_repeat_count,
            "content_anchor_used": anchor_plan is not None,
            "content_anchor_intervals": result.source_intervals if anchor_plan else [],
            "content_anchor_interval_weights": result.source_interval_weights if anchor_plan else [],
            "content_anchor_segment_ids": anchor_plan.segment_ids if anchor_plan else [],
            "content_anchor_threshold": anchor_plan.threshold if anchor_plan else None,
            "content_anchor_capacity_s": anchor_plan.capacity_s if anchor_plan else None,
            "opening_intra_beat_align_used": bool(alignment_mode == "opening" and intra_beat_result and intra_beat_result.used),
            "opening_intra_beat_chunks": intra_beat_result.diagnostics if alignment_mode == "opening" and intra_beat_result else [],
            "opening_intra_beat_replaced_ranges": [list(item) for item in intra_beat_result.replaced_ranges] if alignment_mode == "opening" and intra_beat_result else [],
            "intra_beat_align_used": bool(intra_beat_result and intra_beat_result.used),
            "intra_beat_align_mode": alignment_mode,
            "intra_beat_trigger_drift_s": alignment_trigger_drift,
            "intra_beat_chunks": intra_beat_result.diagnostics if intra_beat_result else [],
            "intra_beat_replaced_ranges": [list(item) for item in intra_beat_result.replaced_ranges] if intra_beat_result else [],
            "hook_leading_guard_used": hook_guard_result.used,
            "hook_leading_min_brightness": args.hook_min_brightness,
            "hook_leading_original_shot": hook_guard_result.original_shot_index,
            "hook_leading_replacement_shots": hook_guard_result.replacement_shot_ids,
            "excluded_end_credit_ids": excluded_end_credit_ids,
        }
        n_dark_fallback_beats += int(bool(result.dark_selected_ids))
        n_capacity_exhausted_beats += int(result.capacity_exhausted)
        n_unused_source_reuse += result.unused_source_reuse_count
        n_overlapping_repeats += result.overlapping_repeat_count
        shots_by_index = {shot.index: shot for shot in shots}
        for shot_index in candidate_shot_ids[beat.beat_id]:
            shot = shots_by_index.get(shot_index)
            if shot is not None:
                candidate_drift_tiers[(beat.beat_id, shot_index)] = chronology_tier(
                    shot,
                    result.source_cursor_start,
                    max_source_drift_s=args.max_source_drift_s,
                )[0]
        if source_start_override is not None:
            result.warnings.append(f"beat {beat.beat_id} opening_story_visual_start {source_start_override:.3f}s")
        if in_opening_guard and args.opening_ordered_fill:
            result.warnings.append(f"beat {beat.beat_id} opening_ordered_fill")
        if in_opening_guard:
            unique_count = len({placement.shot_index for placement in beat_placements})
            if beat_placements and unique_count < args.opening_min_unique_shots:
                result.warnings.append(f"beat {beat.beat_id} opening_low_unique_shots {unique_count} < {args.opening_min_unique_shots}")
            filled_duration = sum(placement.tl_end - placement.tl_start for placement in beat_placements)
            if args.opening_allow_short_fill and filled_duration + 0.02 < timing.duration:
                result.warnings.append(f"beat {beat.beat_id} opening_short_fill {filled_duration:.3f}/{timing.duration:.3f}s")
        if result.widened:
            n_beats_widened += 1
        adjusted_placements = bool((intra_beat_result and intra_beat_result.used) or hook_guard_result.used)
        n_reused += sum(1 for placement in beat_placements if placement.reused) if adjusted_placements else result.reused_count
        n_speedfit += result.speedfit_count
        warnings.extend(result.warnings)
        placements.extend(beat_placements)

    total_duration = max((timing.tl_end for timing in timings), default=0.0)
    before_gap_fill = len(placements)
    placements = fill_timeline_gaps(placements, total_duration, min_visual_clip=args.min_visual_clip, shots=shots)
    pause_fillers = len(placements) - before_gap_fill
    if pause_fillers:
        warnings.append(f"inserted {pause_fillers} pause filler placement(s) to cover TTS inter-beat silence")
        n_reused += pause_fillers
    before_long_split = len(placements)
    placements = split_long_placements(placements, max_clip=args.max_clip)
    long_splits = len(placements) - before_long_split
    if long_splits:
        warnings.append(f"split {long_splits} long placement segment(s) to keep visual clips <= {args.max_clip:.3f}s")
    placements = validate_timeline(placements, total_duration)
    placements = validate_source_bounds(placements, all_shots)
    beat_ids = sorted({timing.beat_id for timing in timings})
    placements_by_beat = {beat_id: [placement for placement in placements if placement.beat_id == beat_id] for beat_id in beat_ids}
    repeat_ratios = []
    n_empty_beats = 0
    n_high_repeat_beats = 0
    for beat_id, beat_placements in placements_by_beat.items():
        if not beat_placements:
            n_empty_beats += 1
            continue
        ratio = sum(1 for placement in beat_placements if placement.reused) / len(beat_placements)
        repeat_ratios.append(ratio)
        if ratio > args.max_repeat_ratio_per_beat:
            n_high_repeat_beats += 1
    coverage_ok = n_empty_beats == 0

    meta = EdlMeta(
        total_duration_s=total_duration,
        n_placements=len(placements),
        n_beats_widened=n_beats_widened,
        n_reused=n_reused,
        n_speedfit=n_speedfit,
        n_intro_excluded=n_intro_excluded if args.exclude_non_story else 0,
        n_empty_beats=n_empty_beats,
        n_high_repeat_beats=n_high_repeat_beats,
        n_dark_fallback_beats=n_dark_fallback_beats,
        n_end_credit_excluded=len(end_credit_shot_ids),
        n_capacity_exhausted_beats=n_capacity_exhausted_beats,
        n_unused_source_reuse=n_unused_source_reuse,
        n_overlapping_repeats=n_overlapping_repeats,
        max_repeat_ratio=round(max(repeat_ratios), 6) if repeat_ratios else 0.0,
        avg_clip_len=round(average_clip_len(placements), 3),
        coverage_ok=coverage_ok,
        warnings=warnings,
        seed=args.seed,
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
        algorithm_version=MATCH_ALGORITHM_VERSION,
    )
    qa = build_edl_qa(
        beats=review_beats,
        placements=placements,
        shots=all_shots,
        semantic_scores=semantic_scores,
        visual_scores=visual_scores,
        weights=weights,
        semantic_result=semantic_result,
        visual_result=visual_result,
        min_semantic_score=args.min_semantic_score,
        warnings=warnings,
        max_repeat_ratio_per_beat=args.max_repeat_ratio_per_beat,
        opening_guard_s=args.opening_guard_s,
        opening_max_repeat_ratio=args.opening_max_repeat_ratio,
        opening_min_unique_shots=args.opening_min_unique_shots,
        review_intents=review_intents,
        story_sections=story_sections,
        match_strategy=args.match_strategy,
        max_source_drift_s=args.max_source_drift_s,
        short_clip_threshold_s=args.min_visual_clip,
        candidate_shot_ids=candidate_shot_ids,
        candidate_drift_tiers=candidate_drift_tiers,
        candidate_diagnostics=candidate_diagnostics,
        end_credit_guard_enabled=args.exclude_end_credits,
        excluded_end_credit_ids=sorted(end_credit_shot_ids),
    )
    sync_qa = build_sync_qa(beats=review_beats, timings=timings, placements=placements, fps=None, short_clip_threshold_s=args.min_visual_clip)
    combined_scores = {
        (beat.beat_id, shot.index): score_shot(
            shot,
            0,
            weights,
            semantic_scores.get((beat.beat_id, shot.index), 0.0),
            visual_scores.get((beat.beat_id, shot.index), 0.0),
        )
        for beat in review_beats
        for shot in shots
    }
    visual_qa = build_visual_qa(
        beats=review_beats,
        placements=placements,
        visual_result=visual_result,
        visual_mode=args.visual_mode,
        candidate_shot_ids=candidate_shot_ids,
        combined_scores=combined_scores,
        candidate_drift_tiers=candidate_drift_tiers,
    )
    write_json(output_path, placements)
    write_json(output_path.with_name("edl.meta.json"), meta)
    write_json(qa_path, qa)
    write_json(sync_qa_path, sync_qa)
    write_json(visual_qa_path, visual_qa)
    maybe_write_review_html(args, output_path, qa)
    cache.write_plan(cache_key, [item.model_dump(mode="json") for item in placements], meta.model_dump(mode="json"), qa, sync_qa=sync_qa, visual_qa=visual_qa)
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
