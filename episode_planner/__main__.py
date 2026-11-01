from __future__ import annotations

import argparse
import logging
from pathlib import Path

from episode_planner.planner import EpisodePlanSettings, build_episode_plan

class EpisodePlannerError(RuntimeError):
    pass

def resolve_optional_file(path: Path | None, *, label: str) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EpisodePlannerError(f"{label} does not exist: {resolved}")
    return resolved

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Episode planner: film_map/story_map -> episode_meta + memory")
    parser.add_argument("--source-path", required=True, type=Path)
    parser.add_argument("--film-map", required=True, type=Path)
    parser.add_argument("--output-meta", required=True, type=Path)
    parser.add_argument("--output-memory", required=True, type=Path)
    parser.add_argument("--series-manifest", default=None, type=Path)
    parser.add_argument("--series-memory-dir", default=None, type=Path)
    parser.add_argument("--episode-key", default=None)
    parser.add_argument("--episode-number", default=None)
    parser.add_argument("--recap-mode", default="auto", choices=["auto", "full", "quick", "merge", "skip"])
    parser.add_argument("--recap-full-threshold", default=0.70, type=float)
    parser.add_argument("--recap-quick-threshold", default=0.35, type=float)
    parser.add_argument("--recap-merge-threshold", default=0.15, type=float)
    parser.add_argument("--quick-target-ratio", default=0.12, type=float)
    parser.add_argument("--quick-min-coverage", default=0.45, type=float)
    parser.add_argument("--video-profile", default=None, type=Path)
    parser.add_argument("--story-map", default=None, type=Path)
    parser.add_argument("--anime-context", default=None, type=Path)
    parser.add_argument("--work-dir", default=Path("work/episode_planner"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser

def run_episode_planner(args: argparse.Namespace) -> int:
    source_path = args.source_path.expanduser().resolve()
    film_map_path = args.film_map.expanduser().resolve()
    output_meta = args.output_meta.expanduser().resolve()
    output_memory = args.output_memory.expanduser().resolve()
    if not source_path.is_file():
        raise EpisodePlannerError(f"source video does not exist: {source_path}")
    if not film_map_path.is_file():
        raise EpisodePlannerError(f"film_map does not exist: {film_map_path}")
    if not 0 <= args.recap_merge_threshold <= args.recap_quick_threshold <= args.recap_full_threshold <= 1:
        raise EpisodePlannerError("thresholds must satisfy 0 <= merge <= quick <= full <= 1")
    if not 0.08 <= args.quick_target_ratio <= 0.15:
        raise EpisodePlannerError("--quick-target-ratio must be between 0.08 and 0.15")
    if not 0 <= args.quick_min_coverage <= 1:
        raise EpisodePlannerError("--quick-min-coverage must be between 0 and 1")
    settings = EpisodePlanSettings(
        recap_mode=args.recap_mode,
        episode_key=args.episode_key,
        episode_number=args.episode_number,
        recap_full_threshold=args.recap_full_threshold,
        recap_quick_threshold=args.recap_quick_threshold,
        recap_merge_threshold=args.recap_merge_threshold,
        quick_target_ratio=args.quick_target_ratio,
        quick_min_coverage=args.quick_min_coverage,
    )
    meta, _memory = build_episode_plan(
        film=source_path,
        film_map_path=film_map_path,
        output_meta_path=output_meta,
        output_memory_path=output_memory,
        settings=settings,
        series_manifest_path=resolve_optional_file(args.series_manifest, label="series manifest"),
        series_memory_dir=args.series_memory_dir.expanduser().resolve() if args.series_memory_dir else None,
        video_profile_path=resolve_optional_file(args.video_profile, label="video profile"),
        story_map_path=resolve_optional_file(args.story_map, label="story map"),
        anime_context_path=resolve_optional_file(args.anime_context, label="anime context"),
    )
    logging.getLogger("episode_planner").info(
        "Done: %s mode=%s score=%.3f",
        output_meta,
        meta.recap_mode,
        meta.importance_score,
    )
    return 0

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_episode_planner(args)
    except (EpisodePlannerError, OSError, ValueError) as exc:
        parser.exit(2, f"episode_planner: error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
