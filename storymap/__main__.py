from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from common.schema import FilmMapMeta, FilmMapSegment, StoryMapMeta, VideoProfile, validate_film_map, validate_story_map, write_json
from common.integrity import file_hash
from storymap.builder import build_story_sections
from storymap.cache import StoryMapCache, stable_hash


class StoryMapError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1.5 story structure map")
    parser.add_argument("--film-map", required=True, type=Path)
    parser.add_argument("--video-profile", default=None, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--content-type", default="movie", choices=["movie", "episode"])
    parser.add_argument("--target-story-sections", default=7, type=int)
    parser.add_argument("--work-dir", default=Path("work/storymap"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_video_profile(path: Path | None) -> VideoProfile | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    return VideoProfile.model_validate_json(resolved.read_text(encoding="utf-8-sig"))


def run_storymap(args: argparse.Namespace) -> int:
    film_map_path = args.film_map.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    output_qa = args.output_qa.expanduser().resolve() if args.output_qa else output_path.with_name("story_map.qa.json")
    work_dir = args.work_dir.expanduser().resolve()
    if not film_map_path.is_file():
        raise StoryMapError(f"film_map does not exist: {film_map_path}")
    if args.target_story_sections <= 0:
        raise StoryMapError("--target-story-sections must be > 0")
    if args.force and work_dir.exists():
        shutil.rmtree(work_dir)
    cache = StoryMapCache(work_dir)
    film_map_raw = json.loads(film_map_path.read_text(encoding="utf-8-sig"))
    film_map = [FilmMapSegment.model_validate(item) for item in film_map_raw]
    meta_path = film_map_path.with_name("film_map.meta.json")
    duration_s = max(segment.tc_end for segment in film_map) if film_map else 0.001
    if meta_path.is_file():
        duration_s = FilmMapMeta.model_validate_json(meta_path.read_text(encoding="utf-8-sig")).duration
    validate_film_map(film_map, duration=duration_s)
    video_profile = load_video_profile(args.video_profile)
    profile_hash = stable_hash(video_profile.model_dump(mode="json") if video_profile else None)
    config_key = stable_hash({
        "film_map": stable_hash(film_map_raw),
        "video_profile": profile_hash,
        "content_type": args.content_type,
        "target_story_sections": args.target_story_sections,
    })
    cached = cache.read_json("story_map.json")
    cached_meta = cache.read_json("story_map.meta.json")
    cached_qa = cache.read_json("story_map.qa.json")
    cache_hits: list[str] = []
    if cached and cached_meta and cached_meta.get("cache_key") == config_key and not args.force:
        sections = cached
        qa = cached_qa or {}
        cache_hits.append("story_map.json")
    else:
        story_sections, report = build_story_sections(
            film_map,
            duration_s=duration_s,
            video_profile=video_profile,
            content_type=args.content_type,
            target_story_sections=args.target_story_sections,
        )
        validate_story_map(story_sections, duration=duration_s)
        sections = [section.model_dump(mode="json") for section in story_sections]
        qa = report.qa
        cache.write_json("story_map.json", sections)
        cache.write_json("story_map.qa.json", qa)
        cache.write_json("story_map.meta.json", {"cache_key": config_key})
    story_sections = validate_story_map([__import__("common.schema", fromlist=["StorySection"]).StorySection.model_validate(item) for item in sections], duration=duration_s)
    meta = StoryMapMeta(
        film_map_path=str(film_map_path),
        video_profile_path=str(args.video_profile.expanduser().resolve()) if args.video_profile else None,
        content_type=args.content_type,
        duration_s=duration_s,
        n_sections=len(story_sections),
        n_non_story=sum(1 for section in story_sections if section.type == "non_story"),
        created_at=datetime.now(timezone.utc),
        cache_hits=cache_hits,
        warnings=qa.get("warnings", []),
        film_map_hash=file_hash(film_map_path),
        video_profile_hash=file_hash(args.video_profile) if args.video_profile else None,
        config_hash=config_key,
        cache_version="storymap-v1",
    )
    write_json(output_path, story_sections)
    write_json(output_path.with_name(f"{output_path.stem}.meta.json"), meta)
    write_json(output_qa, qa)
    logging.getLogger("storymap").info("Done: %s", output_path)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_storymap(args)
    except (StoryMapError, OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"storymap: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
