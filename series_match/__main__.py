from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from common.inputs import load_shots
from common.schema import (
    BeatTiming,
    EdlMeta,
    EdlPlacement,
    EdlSourceMap,
    SeriesReviewBeat,
    SeriesSourceRef,
    Shot,
    validate_beats_timing,
    validate_edl,
    validate_series_review_script,
    write_json,
)


class SeriesMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClipCandidate:
    episode_key: str
    src_key: str
    source_path: str
    shot: Shot
    start: float
    end: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match multi-episode series review beats to footage.")
    parser.add_argument("--series-review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--episode-run-dir", action="append", default=[], help="Episode artifact dir as episode_key=path")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-source-map", required=True, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--min-clip", default=3.0, type=float)
    parser.add_argument("--max-clip", default=5.0, type=float)
    parser.add_argument("--min-visual-clip", default=0.6, type=float)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--work-dir", default=Path("work") / "series_match", type=Path)
    return parser


def parse_episode_run_dirs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise SeriesMatchError("--episode-run-dir must use episode_key=path")
        key, raw_path = value.split("=", 1)
        key = key.strip()
        if not key:
            raise SeriesMatchError("--episode-run-dir episode_key cannot be empty")
        if key in result:
            raise SeriesMatchError(f"duplicate episode run dir for {key}")
        result[key] = Path(raw_path).expanduser().resolve()
    if not result:
        raise SeriesMatchError("at least one --episode-run-dir is required")
    return result


def load_series_beats(path: Path) -> list[SeriesReviewBeat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    beats = [SeriesReviewBeat.model_validate(item) for item in data]
    return validate_series_review_script(beats)


def load_timings(path: Path) -> list[BeatTiming]:
    data = json.loads(path.read_text(encoding="utf-8"))
    timings = [BeatTiming.model_validate(item) for item in data]
    if len(timings) > 1:
        pause_s = timings[1].tl_start - timings[0].tl_end
        pause_s = max(0.0, round(pause_s, 3))
    else:
        pause_s = 0.0
    return validate_beats_timing(timings, pause_s=pause_s)


def source_map_from_beats(beats: list[SeriesReviewBeat]) -> EdlSourceMap:
    sources: dict[str, str] = {}
    for beat in beats:
        for ref in beat.source_refs:
            sources[ref.src] = str(Path(ref.source_path).expanduser().resolve())
    return EdlSourceMap(version=1, sources=sources, created_at=datetime.now(timezone.utc))


def candidate_score(shot: Shot) -> float:
    return float(shot.motion_score or 0.0) * 0.65 + float(shot.brightness or 0.0) * 0.12


def ref_candidates(ref: SeriesSourceRef, shots: list[Shot]) -> list[ClipCandidate]:
    candidates: list[ClipCandidate] = []
    source_path = str(Path(ref.source_path).expanduser().resolve())
    for shot in shots:
        if shot.is_story is False or shot.is_usable is False or shot.is_end_credit is True:
            continue
        start = max(shot.tc_start, ref.src_tc_start)
        end = min(shot.tc_end, ref.src_tc_end)
        if end <= start:
            continue
        candidates.append(
            ClipCandidate(
                episode_key=ref.episode_key,
                src_key=ref.src,
                source_path=source_path,
                shot=shot,
                start=start,
                end=end,
            )
        )
    return sorted(candidates, key=lambda item: (item.start, -candidate_score(item.shot), item.shot.index))


def fallback_candidates(ref: SeriesSourceRef, shots: list[Shot]) -> list[ClipCandidate]:
    source_path = str(Path(ref.source_path).expanduser().resolve())
    values = [
        ClipCandidate(
            episode_key=ref.episode_key,
            src_key=ref.src,
            source_path=source_path,
            shot=shot,
            start=shot.tc_start,
            end=shot.tc_end,
        )
        for shot in shots
        if shot.is_story is not False and shot.is_usable is not False and shot.is_end_credit is not True
    ]
    return sorted(values, key=lambda item: (abs(item.start - ref.src_tc_start), item.start, -candidate_score(item.shot)))


def timing_windows(timings: list[BeatTiming]) -> dict[int, tuple[float, float]]:
    windows: dict[int, tuple[float, float]] = {}
    for index, timing in enumerate(timings):
        end = timings[index + 1].tl_start if index + 1 < len(timings) else timing.tl_end
        windows[timing.beat_id] = (timing.tl_start, end)
    return windows


def add_clip(
    placements: list[EdlPlacement],
    *,
    beat_id: int,
    tl_cursor: float,
    duration: float,
    candidate: ClipCandidate,
) -> None:
    placements.append(
        EdlPlacement(
            tl_start=round(tl_cursor, 3),
            tl_end=round(tl_cursor + duration, 3),
            src=candidate.src_key,
            src_in=round(candidate.start, 3),
            src_out=round(candidate.start + duration, 3),
            beat_id=beat_id,
            shot_index=candidate.shot.index,
            reused=False,
            speed=1.0,
        )
    )


def choose_clip_duration(
    *,
    available: float,
    remaining: float,
    max_clip: float,
    min_visual_clip: float,
) -> float:
    duration = min(max_clip, available, remaining)
    tail_after = remaining - duration
    if 1e-6 < tail_after < min_visual_clip:
        shrink_by = min_visual_clip - tail_after
        if duration - shrink_by >= min_visual_clip:
            duration -= shrink_by
        else:
            # Leave this candidate for a later shot that can absorb the
            # remainder without creating a sub-minimum flash fragment.
            return 0.0
    return duration

def build_edl(
    *,
    beats: list[SeriesReviewBeat],
    timings: list[BeatTiming],
    shots_by_episode: dict[str, list[Shot]],
    min_visual_clip: float,
    max_clip: float,
) -> tuple[list[EdlPlacement], list[str]]:
    windows = timing_windows(timings)
    placements: list[EdlPlacement] = []
    warnings: list[str] = []
    for beat in beats:
        if beat.beat_id not in windows:
            raise SeriesMatchError(f"missing timing for beat {beat.beat_id}")
        tl_cursor, tl_end = windows[beat.beat_id]
        remaining = tl_end - tl_cursor
        candidate_pool: list[ClipCandidate] = []
        for ref in beat.source_refs:
            episode_shots = shots_by_episode.get(ref.episode_key)
            if episode_shots is None:
                raise SeriesMatchError(f"missing shots for episode {ref.episode_key}")
            candidate_pool.extend(ref_candidates(ref, episode_shots))
        if not candidate_pool:
            for ref in beat.source_refs:
                candidate_pool.extend(fallback_candidates(ref, shots_by_episode[ref.episode_key]))
            warnings.append(f"beat {beat.beat_id}: used episode-level fallback candidates")
        used_any = False
        used_shots: set[tuple[str, int]] = set()
        for candidate in candidate_pool:
            if remaining <= 1e-6:
                break
            available = candidate.end - candidate.start
            if available <= 0:
                continue
            duration = choose_clip_duration(
                available=available,
                remaining=remaining,
                max_clip=max_clip,
                min_visual_clip=min_visual_clip,
            )
            if duration + 1e-6 < min_visual_clip:
                continue
            add_clip(placements, beat_id=beat.beat_id, tl_cursor=tl_cursor, duration=duration, candidate=candidate)
            used_shots.add((candidate.src_key, candidate.shot.index))
            tl_cursor += duration
            remaining = tl_end - tl_cursor
            used_any = True
        if remaining > 0.05:
            fallback_pool: list[ClipCandidate] = []
            for ref in beat.source_refs:
                fallback_pool.extend(fallback_candidates(ref, shots_by_episode[ref.episode_key]))
            fallback_pool = sorted(
                fallback_pool,
                key=lambda item: ((item.src_key, item.shot.index) in used_shots, item.start, -candidate_score(item.shot)),
            )
            for candidate in fallback_pool:
                if remaining <= 1e-6:
                    break
                available = candidate.end - candidate.start
                if available <= 0:
                    continue
                duration = choose_clip_duration(
                    available=available,
                    remaining=remaining,
                    max_clip=max_clip,
                    min_visual_clip=min_visual_clip,
                )
                if duration + 1e-6 < min_visual_clip:
                    continue
                add_clip(placements, beat_id=beat.beat_id, tl_cursor=tl_cursor, duration=duration, candidate=candidate)
                used_shots.add((candidate.src_key, candidate.shot.index))
                tl_cursor += duration
                remaining = tl_end - tl_cursor
                used_any = True
            if remaining > 0.05 or not used_any:
                raise SeriesMatchError(f"beat {beat.beat_id} cannot be filled with usable story footage")
            warnings.append(f"beat {beat.beat_id}: used extra fallback footage to fill timing")
    return validate_edl(placements), warnings


def run_series_match(args: argparse.Namespace) -> int:
    if args.max_clip <= 0 or args.min_visual_clip <= 0:
        raise SeriesMatchError("clip lengths must be > 0")
    if args.max_clip < args.min_visual_clip:
        raise SeriesMatchError("--max-clip must be >= --min-visual-clip")
    episode_run_dirs = parse_episode_run_dirs(args.episode_run_dir)
    beats = load_series_beats(args.series_review_script.expanduser().resolve())
    timings = load_timings(args.beats_timing.expanduser().resolve())
    shots_by_episode = {
        episode_key: load_shots(run_dir / "shots.json")
        for episode_key, run_dir in episode_run_dirs.items()
    }
    placements, warnings = build_edl(
        beats=beats,
        timings=timings,
        shots_by_episode=shots_by_episode,
        min_visual_clip=args.min_visual_clip,
        max_clip=args.max_clip,
    )
    source_map = source_map_from_beats(beats)
    write_json(args.output.expanduser().resolve(), placements)
    write_json(args.output_source_map.expanduser().resolve(), source_map)
    clip_lengths = [placement.tl_end - placement.tl_start for placement in placements]
    meta = EdlMeta(
        total_duration_s=round(placements[-1].tl_end if placements else 0.0, 3),
        n_placements=len(placements),
        n_beats_widened=0,
        n_reused=0,
        n_speedfit=0,
        n_intro_excluded=0,
        n_empty_beats=0,
        n_high_repeat_beats=0,
        n_dark_fallback_beats=0,
        n_end_credit_excluded=0,
        n_capacity_exhausted_beats=0,
        n_unused_source_reuse=0,
        n_overlapping_repeats=0,
        max_repeat_ratio=0.0,
        avg_clip_len=round(sum(clip_lengths) / len(clip_lengths), 3) if clip_lengths else 0.0,
        coverage_ok=True,
        warnings=warnings,
        seed=0,
        created_at=datetime.now(timezone.utc),
        cache_hits=[],
        algorithm_version="series-v1",
    )
    write_json(args.output.expanduser().resolve().with_name("edl.meta.json"), meta)
    qa_path = args.output_qa.expanduser().resolve() if args.output_qa else args.output.expanduser().resolve().with_name("edl.qa.json")
    write_json(
        qa_path,
        {
            "version": 1,
            "n_beats": len(beats),
            "n_placements": len(placements),
            "source_count": len(source_map.sources),
            "warnings": warnings,
        },
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_series_match(args)
    except (SeriesMatchError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"series_match: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
