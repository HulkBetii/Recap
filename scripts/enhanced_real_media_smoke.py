from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from array import array
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from common.inputs import load_shots
from common.media import probe_audio_stream_count, probe_duration, probe_video_stream, require_ffmpeg
from common.schema import (
    BeatTiming,
    EditPlan,
    EditPlanQa,
    EdlPlacement,
    EdlSourceMap,
    RenderMeta,
    SeriesReviewBeat,
    Shot,
    validate_edl,
    validate_series_review_script,
    write_json,
)
from postprocess.assets import validate_audio_assets
from scripts.release_helpers import ROOT, resolve_release_work_dir

DEFAULT_SEASON_RUN = Path("runs") / "tensei-shitara-dragon-no-tamago-datta-s01"
DEFAULT_WORK_DIR = Path("work") / "enhanced-real-media-smoke"
OUTPUT_FPS = 30
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
MIN_VISUAL_CLIP_S = 0.6
MAX_VISUAL_CLIP_S = 4.0
MIN_MUSIC_MASTER_CORRELATION = 0.01
MIN_SFX_MASTER_CORRELATION = 0.02
ANALYSIS_WIDTH = 96
ANALYSIS_HEIGHT = 54


class RealMediaSmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokePaths:
    season_run_dir: Path
    production_final_dir: Path
    work_dir: Path
    full_dir: Path
    excerpt_dir: Path
    report: Path


@dataclass(frozen=True)
class ProductionInputs:
    review_script: Path
    event_bank: Path
    beats_timing: Path
    voiceover: Path
    episode_run_dirs: dict[str, Path]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run enhanced post-production acceptance on an existing anime season")
    parser.add_argument("--season-run-dir", type=Path, default=DEFAULT_SEASON_RUN)
    parser.add_argument("--audio-assets", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--excerpt-seconds", type=float, default=90.0)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def resolve_repo_path(path: Path) -> Path:
    return path.expanduser().resolve() if path.is_absolute() else (ROOT / path).resolve()


def build_paths(args: argparse.Namespace) -> SmokePaths:
    season_run_dir = resolve_repo_path(args.season_run_dir)
    try:
        work_dir = resolve_release_work_dir(args.work_dir)
    except ValueError as exc:
        raise RealMediaSmokeError(str(exc)) from exc
    if work_dir == season_run_dir or season_run_dir in work_dir.parents:
        raise RealMediaSmokeError("--work-dir must not be inside the production season run")
    report = resolve_repo_path(args.report) if args.report is not None else work_dir / "report.json"
    if report != work_dir and work_dir not in report.parents:
        raise RealMediaSmokeError("--report must be inside --work-dir")
    return SmokePaths(
        season_run_dir=season_run_dir,
        production_final_dir=season_run_dir / "series_recap",
        work_dir=work_dir,
        full_dir=work_dir / "full",
        excerpt_dir=work_dir / "excerpt",
        report=report,
    )


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise RealMediaSmokeError(f"required production artifact does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_production_inputs(paths: SmokePaths) -> ProductionInputs:
    final_dir = paths.production_final_dir
    review_script = final_dir / "series_review_script.json"
    event_bank = final_dir / "series_event_bank.json"
    beats_timing = final_dir / "beats_timing.json"
    voiceover = final_dir / "voiceover.mp3"
    beats = validate_series_review_script(
        [SeriesReviewBeat.model_validate(item) for item in load_json(review_script)]
    )
    episode_keys = sorted({ref.episode_key for beat in beats for ref in beat.source_refs})
    episode_run_dirs = {key: paths.season_run_dir / key for key in episode_keys}
    missing_shots = [str(run_dir / "shots.json") for run_dir in episode_run_dirs.values() if not (run_dir / "shots.json").is_file()]
    for path in (event_bank, beats_timing, voiceover):
        if not path.is_file():
            raise RealMediaSmokeError(f"required production artifact does not exist: {path}")
    if missing_shots:
        raise RealMediaSmokeError(f"episode shots are missing: {missing_shots[:5]}")
    return ProductionInputs(
        review_script=review_script,
        event_bank=event_bank,
        beats_timing=beats_timing,
        voiceover=voiceover,
        episode_run_dirs=episode_run_dirs,
    )


def run_logged(command: list[str], *, log_path: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n")
        handle.write(result.stdout)
        handle.write(result.stderr)
        handle.write("\n")
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RealMediaSmokeError(f"command failed ({result.returncode}): {message}")


def episode_run_args(episode_run_dirs: dict[str, Path]) -> list[str]:
    result: list[str] = []
    for episode_key, run_dir in sorted(episode_run_dirs.items()):
        result.extend(["--episode-run-dir", f"{episode_key}={run_dir}"])
    return result


def build_match_command(inputs: ProductionInputs, paths: SmokePaths) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "series_match",
        "--series-review-script",
        str(inputs.review_script),
        "--beats-timing",
        str(inputs.beats_timing),
        "--event-bank",
        str(inputs.event_bank),
        "--clip-profile",
        "dynamic_anime",
        "--min-clip",
        "1.5",
        "--max-clip",
        str(MAX_VISUAL_CLIP_S),
        "--min-visual-clip",
        str(MIN_VISUAL_CLIP_S),
        "--output",
        str(paths.full_dir / "edl.json"),
        "--output-source-map",
        str(paths.full_dir / "edl.source_map.json"),
        "--output-qa",
        str(paths.full_dir / "edl.qa.json"),
        "--work-dir",
        str(paths.full_dir / "work" / "series_match"),
    ]
    command.extend(episode_run_args(inputs.episode_run_dirs))
    return command


def build_postprocess_command(
    *,
    edl: Path,
    review_script: Path,
    event_bank: Path,
    beats_timing: Path,
    source_map: Path,
    audio_assets: Path,
    episode_run_dirs: dict[str, Path],
    output_dir: Path,
    seed: int,
    overrides: Path | None = None,
    force: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "postprocess",
        "--edl",
        str(edl),
        "--series-review-script",
        str(review_script),
        "--event-bank",
        str(event_bank),
        "--beats-timing",
        str(beats_timing),
        "--source-map",
        str(source_map),
        "--audio-assets",
        str(audio_assets),
        "--seed",
        str(seed),
        "--profile",
        "dynamic_anime",
        "--output",
        str(output_dir / "edit_plan.json"),
        "--output-meta",
        str(output_dir / "edit_plan.meta.json"),
        "--output-qa",
        str(output_dir / "edit_plan.qa.json"),
        "--output-attribution",
        str(output_dir / "audio_attribution.txt"),
        "--work-dir",
        str(output_dir / "work" / "postprocess"),
    ]
    command.extend(episode_run_args(episode_run_dirs))
    if overrides is not None:
        command.extend(["--overrides", str(overrides)])
    if force:
        command.append("--force")
    return command


def build_render_command(*, paths: SmokePaths, audio_assets: Path) -> list[str]:
    excerpt = paths.excerpt_dir
    return [
        sys.executable,
        "-m",
        "render",
        "--edl",
        str(excerpt / "edl.json"),
        "--voiceover",
        str(excerpt / "voiceover.mp3"),
        "--source-map",
        str(excerpt / "edl.source_map.json"),
        "--edit-plan",
        str(excerpt / "edit_plan.json"),
        "--audio-assets",
        str(audio_assets),
        "--output",
        str(excerpt / "enhanced-real-smoke.mp4"),
        "--width",
        str(OUTPUT_WIDTH),
        "--height",
        str(OUTPUT_HEIGHT),
        "--fps",
        str(OUTPUT_FPS),
        "--crf",
        "28",
        "--preset",
        "ultrafast",
        "--concurrency",
        "2",
        "--work-dir",
        str(excerpt / "work" / "render"),
        "--force",
    ]


def _source_episode_map(beats: Sequence[SeriesReviewBeat]) -> dict[str, str]:
    return {ref.src: ref.episode_key for beat in beats for ref in beat.source_refs}


def _adjacency_counts(placements: Sequence[EdlPlacement]) -> tuple[int, int]:
    same_shot = 0
    contiguous = 0
    for previous, current in zip(placements, placements[1:]):
        if previous.src != current.src:
            continue
        if previous.shot_index == current.shot_index:
            same_shot += 1
        if abs(previous.src_out - current.src_in) <= 1e-6 or abs(current.src_out - previous.src_in) <= 1e-6:
            contiguous += 1
    return same_shot, contiguous


def validate_dynamic_outputs(
    *,
    edl_path: Path,
    qa_path: Path,
    beats_path: Path,
    timings_path: Path,
    episode_run_dirs: dict[str, Path],
) -> dict[str, Any]:
    placements = validate_edl([EdlPlacement.model_validate(item) for item in load_json(edl_path)])
    if not placements:
        raise RealMediaSmokeError("dynamic matcher produced an empty EDL")
    beats = validate_series_review_script([SeriesReviewBeat.model_validate(item) for item in load_json(beats_path)])
    timings = [BeatTiming.model_validate(item) for item in load_json(timings_path)]
    if abs(placements[-1].tl_end - timings[-1].tl_end) > 0.05:
        raise RealMediaSmokeError("dynamic EDL does not tile the full voiceover timeline")
    durations = [placement.tl_end - placement.tl_start for placement in placements]
    if max(durations) > MAX_VISUAL_CLIP_S + 1e-6:
        raise RealMediaSmokeError("dynamic EDL contains a placement longer than 4 seconds")
    if min(durations) < MIN_VISUAL_CLIP_S - 1e-6:
        raise RealMediaSmokeError("dynamic EDL contains a placement below the 0.6 second hard floor")

    source_episodes = _source_episode_map(beats)
    shots_by_episode = {
        key: {shot.index: shot for shot in load_shots(run_dir / "shots.json")}
        for key, run_dir in episode_run_dirs.items()
    }
    non_story: list[int] = []
    end_credit: list[int] = []
    out_of_bounds: list[int] = []
    for index, placement in enumerate(placements):
        episode_key = source_episodes.get(placement.src)
        shots = shots_by_episode.get(episode_key or "")
        if shots is None or placement.shot_index not in shots:
            raise RealMediaSmokeError(f"placement #{index} cannot resolve its indexed shot")
        shot = shots[placement.shot_index]
        if not shot.is_story:
            non_story.append(index)
        if shot.is_end_credit:
            end_credit.append(index)
        if placement.src_in < shot.tc_start - 1e-3 or placement.src_out > shot.tc_end + 1e-3:
            out_of_bounds.append(index)
    if non_story:
        raise RealMediaSmokeError(f"dynamic EDL selected non-story shots: {non_story[:10]}")
    if end_credit:
        raise RealMediaSmokeError(f"dynamic EDL selected end-credit shots: {end_credit[:10]}")
    if out_of_bounds:
        raise RealMediaSmokeError(f"dynamic EDL exceeds indexed shot bounds: {out_of_bounds[:10]}")

    qa = load_json(qa_path)
    if qa.get("algorithm_version") != "series-v3-dynamic-anime" or qa.get("clip_profile") != "dynamic_anime":
        raise RealMediaSmokeError("matcher QA does not identify the dynamic anime algorithm")
    same_shot, contiguous = _adjacency_counts(placements)
    if int(qa.get("n_adjacent_repeat_fallbacks", -1)) != same_shot:
        raise RealMediaSmokeError("adjacent same-shot reuse is not fully covered by QA diagnostics")
    if int(qa.get("n_contiguous_source_fallbacks", -1)) != contiguous:
        raise RealMediaSmokeError("contiguous source reuse is not fully covered by QA diagnostics")
    return {
        "n_placements": len(placements),
        "duration_s": round(placements[-1].tl_end, 3),
        "min_clip_s": round(min(durations), 3),
        "max_clip_s": round(max(durations), 3),
        "adjacent_same_shot_fallbacks": same_shot,
        "contiguous_source_fallbacks": contiguous,
        "non_story_selected": 0,
        "end_credit_selected": 0,
    }


def _cue_spacing(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    return min(current - previous for previous, current in zip(values, values[1:]))


def max_cues_in_rolling_window(values: Sequence[float], window_s: float) -> int:
    ordered = sorted(values)
    maximum = 0
    end = 0
    for start, timestamp in enumerate(ordered):
        end = max(end, start)
        while end < len(ordered) and ordered[end] < timestamp + window_s - 1e-9:
            end += 1
        maximum = max(maximum, end - start)
    return maximum


def validate_postprocess_outputs(
    *,
    plan_path: Path,
    qa_path: Path,
    edl_path: Path,
    require_zoom: bool = False,
    require_freeze: bool = False,
    require_sfx: bool = False,
) -> dict[str, Any]:
    plan = EditPlan.model_validate(load_json(plan_path))
    qa = EditPlanQa.model_validate(load_json(qa_path))
    placements = validate_edl([EdlPlacement.model_validate(item) for item in load_json(edl_path)])
    if len(plan.placements) != len(placements):
        raise RealMediaSmokeError("edit plan placement count differs from the EDL")
    failed_bounds = [
        check
        for check in qa.source_bound_checks
        if not bool(check.get("in_bounds", check.get("effect_in_bounds")))
        or not bool(check.get("effect_in_bounds", True))
    ]
    if failed_bounds:
        raise RealMediaSmokeError(f"postprocess source-bound checks failed: {failed_bounds[:5]}")
    cues = sorted(plan.music_cues, key=lambda item: (item.tl_start, item.tl_end))
    if not cues or abs(cues[0].tl_start) > 0.05 or abs(cues[-1].tl_end - plan.total_duration_s) > 0.05:
        raise RealMediaSmokeError("music cues do not cover the full edit timeline")
    if any(current.tl_start > previous.tl_end + 0.05 for previous, current in zip(cues, cues[1:])):
        raise RealMediaSmokeError("music cues contain a timeline gap")
    whoosh_times = sorted(cue.tl_start for cue in plan.sfx_cues if cue.kind == "whoosh")
    impact_times = sorted(cue.tl_start for cue in plan.sfx_cues if cue.kind == "impact")
    whoosh_spacing = _cue_spacing(whoosh_times)
    impact_spacing = _cue_spacing(impact_times)
    if whoosh_spacing is not None and whoosh_spacing < 6.0 - 1e-6:
        raise RealMediaSmokeError("whoosh cues violate the 6 second spacing rule")
    if impact_spacing is not None and impact_spacing < 15.0 - 1e-6:
        raise RealMediaSmokeError("impact cues violate the 15 second spacing rule")
    whoosh_per_minute = len(whoosh_times) / (plan.total_duration_s / 60.0)
    rolling_whoosh_max = max_cues_in_rolling_window(whoosh_times, 60.0)
    if rolling_whoosh_max > 6:
        raise RealMediaSmokeError("whoosh cue density exceeds 6 cues in a rolling 60 second window")
    frozen = [
        (placement.beat_id, placements[placement.placement_index].tl_end - placement.freeze_duration_s)
        for placement in plan.placements
        if placement.freeze_duration_s > 0
    ]
    if len({beat_id for beat_id, _ in frozen}) != len(frozen):
        raise RealMediaSmokeError("edit plan contains more than one freeze in a beat")
    freeze_spacing = _cue_spacing(sorted(timestamp for _, timestamp in frozen))
    if freeze_spacing is not None and freeze_spacing < 15.0 - 1e-6:
        raise RealMediaSmokeError("freeze cues violate the 15 second spacing rule")
    coverage_ratio = float(qa.cue_density.get("music_coverage_ratio", 0.0))
    if coverage_ratio < 0.999:
        raise RealMediaSmokeError("postprocess QA reports incomplete music coverage")
    zoom_count = sum(edit.zoom_start != 1.0 or edit.zoom_end != 1.0 for edit in plan.placements)
    if require_zoom and zoom_count == 0:
        raise RealMediaSmokeError("acceptance excerpt has no planned zoom or push-in to measure")
    if require_freeze and not frozen:
        raise RealMediaSmokeError("acceptance excerpt has no planned freeze to measure")
    if require_sfx and not plan.sfx_cues:
        raise RealMediaSmokeError("acceptance excerpt has no planned SFX cue to measure")
    return {
        "n_zoom": zoom_count,
        "n_speed_ramp": sum(bool(edit.speed_ramp) for edit in plan.placements),
        "n_freeze": len(frozen),
        "n_music_cues": len(plan.music_cues),
        "n_whoosh": len(whoosh_times),
        "n_impact": len(impact_times),
        "whoosh_per_minute": round(whoosh_per_minute, 3),
        "rolling_60s_whoosh_max": rolling_whoosh_max,
        "asset_warnings": qa.asset_warnings,
    }


def safe_excerpt_cutoff(
    placements: Sequence[EdlPlacement], requested_duration_s: float, *, fps: int = OUTPUT_FPS
) -> float:
    if requested_duration_s <= 0:
        raise RealMediaSmokeError("--excerpt-seconds must be greater than zero")
    if not placements or placements[-1].tl_end + 1e-6 < requested_duration_s:
        raise RealMediaSmokeError(f"season timeline is shorter than the requested {requested_duration_s:.3f}s excerpt")
    requested_frame = round(requested_duration_s * fps)
    max_offset = math.ceil(MAX_VISUAL_CLIP_S * fps)
    offsets = sorted(range(-max_offset, max_offset + 1), key=lambda value: (abs(value), value < 0))
    for offset in offsets:
        candidate = (requested_frame + offset) / fps
        if candidate <= 0 or candidate > placements[-1].tl_end + 1e-6:
            continue
        final = next(
            (
                placement
                for placement in placements
                if placement.tl_start + 1e-6 < candidate <= placement.tl_end + 1e-6
            ),
            None,
        )
        if final is not None and candidate - final.tl_start >= MIN_VISUAL_CLIP_S - 1e-6:
            return round(candidate, 6)
    raise RealMediaSmokeError("could not find a frame-locked excerpt cutoff that preserves the 0.6 second floor")


def build_excerpt_artifacts(
    *,
    full_edl_path: Path,
    full_source_map_path: Path,
    review_script_path: Path,
    beats_timing_path: Path,
    output_dir: Path,
    duration_s: float,
) -> tuple[list[EdlPlacement], list[SeriesReviewBeat], list[BeatTiming]]:
    full_edl = validate_edl([EdlPlacement.model_validate(item) for item in load_json(full_edl_path)])
    duration_s = safe_excerpt_cutoff(full_edl, duration_s)
    excerpt_edl: list[EdlPlacement] = []
    for placement in full_edl:
        if placement.tl_start >= duration_s - 1e-6:
            break
        tl_end = min(placement.tl_end, duration_s)
        source_duration = (tl_end - placement.tl_start) * placement.speed
        payload = placement.model_dump(mode="json")
        payload.update({"tl_end": round(tl_end, 6), "src_out": round(placement.src_in + source_duration, 6)})
        excerpt_edl.append(EdlPlacement.model_validate(payload))
    excerpt_edl = validate_edl(excerpt_edl, total_duration=duration_s)
    if excerpt_edl[-1].tl_end - excerpt_edl[-1].tl_start < MIN_VISUAL_CLIP_S - 1e-6:
        raise RealMediaSmokeError("excerpt cutoff created a final placement below the 0.6 second hard floor")
    selected_beat_ids = {placement.beat_id for placement in excerpt_edl}
    beats = validate_series_review_script(
        [
            SeriesReviewBeat.model_validate(item)
            for item in load_json(review_script_path)
            if int(item["beat_id"]) in selected_beat_ids
        ]
    )
    timings: list[BeatTiming] = []
    for item in load_json(beats_timing_path):
        timing = BeatTiming.model_validate(item)
        if timing.beat_id not in selected_beat_ids:
            continue
        tl_end = min(timing.tl_end, duration_s)
        if tl_end <= timing.tl_start:
            continue
        payload = timing.model_dump(mode="json")
        payload.update({"tl_end": round(tl_end, 6), "duration": round(tl_end - timing.tl_start, 6)})
        timings.append(BeatTiming.model_validate(payload))
    if {beat.beat_id for beat in beats} != {timing.beat_id for timing in timings}:
        raise RealMediaSmokeError("excerpt review/timing beat ids do not match")
    full_source_map = EdlSourceMap.model_validate(load_json(full_source_map_path))
    used_sources = {placement.src for placement in excerpt_edl}
    excerpt_source_map = EdlSourceMap(
        version=full_source_map.version,
        sources={key: value for key, value in full_source_map.sources.items() if key in used_sources},
        created_at=full_source_map.created_at,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "edl.json", excerpt_edl)
    write_json(output_dir / "series_review_script.json", beats)
    write_json(output_dir / "beats_timing.json", timings)
    write_json(output_dir / "edl.source_map.json", excerpt_source_map)
    return excerpt_edl, beats, timings


def write_acceptance_freeze_override(
    *,
    edl_path: Path,
    review_script_path: Path,
    episode_run_dirs: dict[str, Path],
    output_path: Path,
) -> int:
    placements = validate_edl([EdlPlacement.model_validate(item) for item in load_json(edl_path)])
    beats = validate_series_review_script(
        [SeriesReviewBeat.model_validate(item) for item in load_json(review_script_path)]
    )
    source_episodes = _source_episode_map(beats)
    shots_by_episode = {
        key: {shot.index: shot for shot in load_shots(run_dir / "shots.json")}
        for key, run_dir in episode_run_dirs.items()
    }
    candidates: list[tuple[float, int]] = []
    for index, placement in enumerate(placements):
        episode_key = source_episodes.get(placement.src)
        shot = shots_by_episode.get(episode_key or "", {}).get(placement.shot_index)
        duration = placement.tl_end - placement.tl_start
        if duration >= 2.0 and not placement.reused and shot is not None and shot.motion_score > 0.01:
            candidates.append((-shot.motion_score, index))
    if not candidates:
        raise RealMediaSmokeError("first excerpt has no eligible moving placement for acceptance freeze override")
    _, placement_index = min(candidates)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {"version": 1, "placements": [{"placement_index": placement_index, "freeze": True}]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return placement_index


def trim_voiceover(
    *, voiceover: Path, output: Path, duration_s: float, log_path: Path, env: dict[str, str]
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run_logged(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(voiceover),
            "-vn",
            "-af",
            f"atrim=duration={duration_s:.6f},asetpts=PTS-STARTPTS",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output),
        ],
        log_path=log_path,
        env=env,
    )
    actual = probe_duration(output)
    if abs(actual - duration_s) > 0.05:
        raise RealMediaSmokeError(f"trimmed voiceover duration mismatch: {actual:.3f}s != {duration_s:.3f}s")


def capture_bytes(command: list[str]) -> bytes:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() or "unknown error"
        raise RealMediaSmokeError(f"media analysis command failed ({result.returncode}): {message}")
    return result.stdout


def pcm_samples(path: Path, *, sample_rate: int = 8000) -> array[int]:
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    samples = array("h")
    samples.frombytes(raw)
    return samples


def sample_window_rms(samples: Sequence[int], start_s: float, end_s: float, *, sample_rate: int = 8000) -> float:
    start = max(0, round(start_s * sample_rate))
    end = min(len(samples), round(end_s * sample_rate))
    if end <= start:
        return 0.0
    energy = sum((sample / 32768.0) ** 2 for sample in samples[start:end])
    return math.sqrt(energy / (end - start))


def sample_window_correlation(
    first: Sequence[int],
    second: Sequence[int],
    start_s: float,
    end_s: float,
    *,
    sample_rate: int = 8000,
) -> float:
    start = max(0, round(start_s * sample_rate))
    end = min(len(first), len(second), round(end_s * sample_rate))
    if end <= start:
        return 0.0
    first_window = first[start:end]
    second_window = second[start:end]
    first_mean = sum(first_window) / len(first_window)
    second_mean = sum(second_window) / len(second_window)
    covariance = sum(
        (left - first_mean) * (right - second_mean)
        for left, right in zip(first_window, second_window, strict=True)
    )
    first_energy = sum((value - first_mean) ** 2 for value in first_window)
    second_energy = sum((value - second_mean) ** 2 for value in second_window)
    if first_energy <= 0 or second_energy <= 0:
        return 0.0
    return covariance / math.sqrt(first_energy * second_energy)


def audio_packet_hash(path: Path) -> str:
    payload = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "adts",
            "pipe:1",
        ]
    )
    if not payload:
        raise RealMediaSmokeError(f"audio packet extraction returned no data: {path}")
    return hashlib.sha256(payload).hexdigest()


def _single_cache_file(audio_cache: Path, prefix: str) -> Path:
    matches = list(audio_cache.glob(f"{prefix}-*.m4a"))
    if len(matches) != 1:
        raise RealMediaSmokeError(f"expected exactly one {prefix} cache artifact, found {len(matches)}")
    return matches[0]


def analyze_enhanced_audio(
    *,
    output: Path,
    plan: EditPlan,
    render_work_dir: Path,
    source_map: EdlSourceMap,
    require_sfx: bool,
) -> dict[str, Any]:
    audio_cache = render_work_dir / "audio_cache"
    music_path = _single_cache_file(audio_cache, "music")
    sfx_path = _single_cache_file(audio_cache, "sfx")
    master_path = _single_cache_file(audio_cache, "master")
    video_only = render_work_dir / "video_only.mp4"
    if probe_audio_stream_count(video_only) != 0:
        raise RealMediaSmokeError("video-only concat unexpectedly contains an audio stream")
    temp_clips = list((render_work_dir / "temp_clips").glob("*.mp4"))
    if not temp_clips or any(probe_audio_stream_count(path) != 0 for path in temp_clips):
        raise RealMediaSmokeError("render temp clips do not prove video-only source handling")
    source_audio_streams = sum(probe_audio_stream_count(Path(value)) for value in source_map.sources.values())
    if source_audio_streams == 0:
        raise RealMediaSmokeError("source media has no audio stream, so source-audio exclusion cannot be exercised")
    if audio_packet_hash(output) != audio_packet_hash(master_path):
        raise RealMediaSmokeError("final output audio packets do not match the enhanced master cache")

    music_samples = pcm_samples(music_path)
    master_samples = pcm_samples(master_path)
    music_window_s = 5.0
    music_rms = [
        sample_window_rms(music_samples, start, min(plan.total_duration_s, start + music_window_s))
        for start in [index * music_window_s for index in range(math.ceil(plan.total_duration_s / music_window_s))]
    ]
    if not music_rms or min(music_rms) <= 1e-5:
        raise RealMediaSmokeError("rendered music bed is silent in at least one timeline window")
    music_master_cue_correlations: list[dict[str, Any]] = []
    for index, cue in enumerate(plan.music_cues):
        analysis_start = cue.tl_start + (cue.crossfade_s / 2 if index > 0 else 0.0)
        next_crossfade = plan.music_cues[index + 1].crossfade_s if index + 1 < len(plan.music_cues) else 0.0
        analysis_end = cue.tl_end - next_crossfade / 2
        if analysis_end - analysis_start < 0.5:
            analysis_start, analysis_end = cue.tl_start, cue.tl_end
        correlation = abs(
            sample_window_correlation(music_samples, master_samples, analysis_start, analysis_end)
        )
        music_master_cue_correlations.append(
            {
                "asset_id": cue.asset_id,
                "tl_start_s": round(cue.tl_start, 3),
                "tl_end_s": round(cue.tl_end, 3),
                "analysis_start_s": round(analysis_start, 3),
                "analysis_end_s": round(analysis_end, 3),
                "correlation": round(correlation, 6),
            }
        )
    failed_music_cues = [
        item
        for item in music_master_cue_correlations
        if float(item["correlation"]) < MIN_MUSIC_MASTER_CORRELATION
    ]
    if failed_music_cues:
        raise RealMediaSmokeError(
            "enhanced master does not contain a measurable contribution from every music cue: "
            f"{failed_music_cues[:5]}"
        )
    music_master_correlation = min(
        float(item["correlation"]) for item in music_master_cue_correlations
    )

    sfx_samples = pcm_samples(sfx_path)
    cue_rms = [
        sample_window_rms(sfx_samples, cue.tl_start, min(plan.total_duration_s, cue.tl_start + 1.0))
        for cue in plan.sfx_cues
    ]
    if require_sfx and not cue_rms:
        raise RealMediaSmokeError("no rendered SFX cue is available for audibility validation")
    if any(value <= 1e-5 for value in cue_rms):
        raise RealMediaSmokeError("one or more selected SFX cues are silent at their planned timeline position")
    sfx_master_correlations = [
        abs(
            sample_window_correlation(
                sfx_samples,
                master_samples,
                cue.tl_start,
                min(plan.total_duration_s, cue.tl_start + 1.0),
            )
        )
        for cue in plan.sfx_cues
    ]
    if any(value < MIN_SFX_MASTER_CORRELATION for value in sfx_master_correlations):
        raise RealMediaSmokeError("enhanced master does not contain a measurable contribution from every SFX cue")
    return {
        "music_cache": music_path.name,
        "sfx_cache": sfx_path.name,
        "master_cache": master_path.name,
        "music_min_5s_rms": round(min(music_rms), 8),
        "music_master_correlation": round(music_master_correlation, 6),
        "music_master_cue_correlations": music_master_cue_correlations,
        "sfx_cue_rms": [round(value, 8) for value in cue_rms],
        "sfx_master_correlations": [round(value, 6) for value in sfx_master_correlations],
        "source_audio_streams_exercised": source_audio_streams,
        "video_only_audio_streams": 0,
        "temp_video_audio_streams": 0,
        "final_audio_matches_master_packets": True,
    }


def video_frames(path: Path) -> list[bytes]:
    frame_size = ANALYSIS_WIDTH * ANALYSIS_HEIGHT
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    if len(raw) % frame_size:
        raise RealMediaSmokeError("decoded output contains a partial frame")
    return [raw[offset : offset + frame_size] for offset in range(0, len(raw), frame_size)]


def single_frame(path: Path, timestamp_s: float) -> bytes:
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{timestamp_s:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    expected = ANALYSIS_WIDTH * ANALYSIS_HEIGHT
    if len(raw) != expected:
        raise RealMediaSmokeError(f"expected one decoded frame, got {len(raw)} bytes")
    return raw


def mean_absolute_difference(first: bytes, second: bytes) -> float:
    if len(first) != len(second):
        raise RealMediaSmokeError("frame sizes do not match")
    return sum(abs(left - right) for left, right in zip(first, second)) / len(first)


def analyze_picture(
    *,
    output: Path,
    plan: EditPlan,
    edl: Sequence[EdlPlacement],
    source_map: EdlSourceMap,
    require_zoom: bool,
    require_freeze: bool,
) -> dict[str, Any]:
    frames = video_frames(output)
    expected_frames = round(plan.total_duration_s * OUTPUT_FPS)
    if len(frames) != expected_frames:
        raise RealMediaSmokeError(f"decoded frame count mismatch: {len(frames)} != {expected_frames}")
    mean_luma = [sum(frame) / len(frame) for frame in frames]
    black_ratios = [sum(value < 8 for value in frame) / len(frame) for frame in frames]
    black_indexes = [
        index for index, (mean, ratio) in enumerate(zip(mean_luma, black_ratios, strict=True))
        if mean <= 0.5 or ratio >= 0.995
    ]
    black_runs: list[int] = []
    previous_black_index: int | None = None
    for index in black_indexes:
        if previous_black_index is None or index != previous_black_index + 1:
            black_runs.append(1)
        else:
            black_runs[-1] += 1
        previous_black_index = index
    isolated_black = [
        index
        for index in range(1, len(frames) - 1)
        if mean_luma[index] < 3.0 and min(mean_luma[index - 1], mean_luma[index + 1]) > 12.0
    ]
    if black_indexes:
        raise RealMediaSmokeError(
            "rendered excerpt contains black frames: "
            f"indexes={black_indexes[:10]}, longest_run={max(black_runs, default=0)}"
        )
    if isolated_black:
        raise RealMediaSmokeError(f"rendered excerpt contains isolated black flash frames: {isolated_black[:10]}")

    zoom_mad: float | None = None
    zoom_growth: float | None = None
    push_in_candidates = [
        edit
        for edit in plan.placements
        if not edit.disabled
        and not edit.speed_ramp
        and edit.zoom_end > edit.zoom_start + 0.01
    ]
    if push_in_candidates:
        edit = push_in_candidates[0]
        placement = edl[edit.placement_index]
        moving_duration = placement.tl_end - placement.tl_start - edit.freeze_duration_s
        early_local = max(0.05, moving_duration * 0.2)
        late_local = max(early_local + 0.05, moving_duration * 0.75)
        source_path = Path(source_map.sources[placement.src])
        early_output = single_frame(output, placement.tl_start + early_local)
        late_output = single_frame(output, placement.tl_start + late_local)
        early_source = single_frame(source_path, placement.src_in + early_local * placement.speed)
        late_source = single_frame(source_path, placement.src_in + late_local * placement.speed)
        early_mad = mean_absolute_difference(early_output, early_source)
        late_mad = mean_absolute_difference(late_output, late_source)
        zoom_mad = late_mad
        zoom_growth = late_mad / max(early_mad, 1e-6)
        if late_mad <= 1.0 or zoom_growth <= 1.03:
            raise RealMediaSmokeError("planned push-in growth is not observable in the rendered excerpt")
    else:
        zoom_candidates = [
            edit
            for edit in plan.placements
            if not edit.disabled
            and not edit.speed_ramp
            and edit.freeze_duration_s == 0
            and (abs(edit.zoom_start - 1.0) > 1e-6 or abs(edit.zoom_end - 1.0) > 1e-6)
        ]
        if zoom_candidates:
            edit = zoom_candidates[0]
            placement = edl[edit.placement_index]
            local_s = (placement.tl_end - placement.tl_start) * 0.5
            output_frame = single_frame(output, placement.tl_start + local_s)
            source_path = Path(source_map.sources[placement.src])
            source_frame = single_frame(source_path, placement.src_in + local_s * placement.speed)
            zoom_mad = mean_absolute_difference(output_frame, source_frame)
            if zoom_mad <= 1.0:
                raise RealMediaSmokeError("planned zoom is not observable in the rendered excerpt")
    if require_zoom and zoom_mad is None:
        raise RealMediaSmokeError("rendered excerpt has no zoom or push-in available for measurement")

    freeze_motion: float | None = None
    moving_motion: float | None = None
    freeze_candidates = [edit for edit in plan.placements if not edit.disabled and edit.freeze_duration_s > 0]
    if freeze_candidates:
        edit = freeze_candidates[0]
        placement = edl[edit.placement_index]
        freeze_start = max(1, round((placement.tl_end - edit.freeze_duration_s) * OUTPUT_FPS) + 2)
        freeze_end = min(len(frames), round(placement.tl_end * OUTPUT_FPS) - 1)
        differences = [
            mean_absolute_difference(frames[index - 1], frames[index])
            for index in range(freeze_start, freeze_end)
        ]
        if not differences:
            raise RealMediaSmokeError("planned freeze does not contain enough output frames to analyze")
        freeze_motion = sum(differences) / len(differences)
        moving_start = max(round(placement.tl_start * OUTPUT_FPS) + 1, freeze_start - len(differences) - 3)
        moving_differences = [
            mean_absolute_difference(frames[index - 1], frames[index])
            for index in range(moving_start, freeze_start - 1)
        ]
        if not moving_differences:
            raise RealMediaSmokeError("planned freeze has no preceding moving window to compare")
        moving_motion = sum(moving_differences) / len(moving_differences)
        if moving_motion <= 0.05 or freeze_motion >= moving_motion * 0.7:
            raise RealMediaSmokeError("planned freeze is not observable in the rendered excerpt")
    if require_freeze and freeze_motion is None:
        raise RealMediaSmokeError("rendered excerpt has no freeze available for measurement")
    return {
        "decoded_frames": len(frames),
        "min_mean_luma": round(min(mean_luma), 3),
        "max_black_pixel_ratio": round(max(black_ratios), 4),
        "black_frame_count": len(black_indexes),
        "longest_black_run_frames": max(black_runs, default=0),
        "isolated_black_frames": len(isolated_black),
        "zoom_mad": round(zoom_mad, 3) if zoom_mad is not None else None,
        "zoom_growth_ratio": round(zoom_growth, 3) if zoom_growth is not None else None,
        "moving_frame_mad": round(moving_motion, 3) if moving_motion is not None else None,
        "freeze_frame_mad": round(freeze_motion, 3) if freeze_motion is not None else None,
    }


def validate_render_output(
    *,
    paths: SmokePaths,
    duration_s: float,
    require_zoom: bool,
    require_freeze: bool,
    require_sfx: bool,
) -> dict[str, Any]:
    output = paths.excerpt_dir / "enhanced-real-smoke.mp4"
    stream = probe_video_stream(output)
    actual_duration = probe_duration(output)
    audio_stream_count = probe_audio_stream_count(output)
    if int(stream["width"]) != OUTPUT_WIDTH or int(stream["height"]) != OUTPUT_HEIGHT:
        raise RealMediaSmokeError("rendered excerpt is not 1920x1080")
    if abs(float(stream["fps"]) - OUTPUT_FPS) > 0.01:
        raise RealMediaSmokeError("rendered excerpt is not frame-locked at 30 fps")
    expected_frames = round(duration_s * OUTPUT_FPS)
    frame_count = stream.get("frame_count")
    if frame_count is not None and int(frame_count) != expected_frames:
        raise RealMediaSmokeError(f"ffprobe frame count mismatch: {frame_count} != {expected_frames}")
    if abs(actual_duration - duration_s) > 0.05:
        raise RealMediaSmokeError(f"rendered duration mismatch: {actual_duration:.3f}s != {duration_s:.3f}s")
    if audio_stream_count != 1:
        raise RealMediaSmokeError(f"rendered excerpt must contain one audio stream, found {audio_stream_count}")
    meta = RenderMeta.model_validate(load_json(paths.excerpt_dir / "render.meta.json"))
    if meta.original_audio_included is not False or meta.audio_stream_count != 1:
        raise RealMediaSmokeError("render metadata does not confirm one-stream source-audio exclusion")
    plan = EditPlan.model_validate(load_json(paths.excerpt_dir / "edit_plan.json"))
    edl = validate_edl([EdlPlacement.model_validate(item) for item in load_json(paths.excerpt_dir / "edl.json")])
    source_map = EdlSourceMap.model_validate(load_json(paths.excerpt_dir / "edl.source_map.json"))
    picture = analyze_picture(
        output=output,
        plan=plan,
        edl=edl,
        source_map=source_map,
        require_zoom=require_zoom,
        require_freeze=require_freeze,
    )
    audio = analyze_enhanced_audio(
        output=output,
        plan=plan,
        render_work_dir=paths.excerpt_dir / "work" / "render",
        source_map=source_map,
        require_sfx=require_sfx,
    )
    return {
        "output": str(output),
        "duration_s": round(actual_duration, 3),
        "frame_count": expected_frames,
        "audio_stream_count": audio_stream_count,
        "original_audio_included": False,
        "picture": picture,
        "audio": audio,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_signatures(inputs: ProductionInputs, source_map_path: Path | None = None) -> dict[str, str]:
    paths = [inputs.review_script, inputs.event_bank, inputs.beats_timing, inputs.voiceover]
    paths.extend(run_dir / "shots.json" for run_dir in inputs.episode_run_dirs.values())
    if source_map_path is not None:
        paths.append(source_map_path)
    return {
        str(path.resolve()): sha256_file(path) if path.is_file() else "<missing>"
        for path in paths
    }


@contextmanager
def production_artifact_guard(
    inputs: ProductionInputs, source_map_path: Path, *, failure_report: Path
) -> Any:
    before = artifact_signatures(inputs, source_map_path)
    try:
        yield
    finally:
        after = artifact_signatures(inputs, source_map_path)
        changed = sorted(path for path, digest in before.items() if after.get(path) != digest)
        if changed:
            failure_report.parent.mkdir(parents=True, exist_ok=True)
            failure_report.write_text(
                json.dumps(
                    {"status": "failed", "reason": "production_artifact_mutation", "changed_paths": changed},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            raise RealMediaSmokeError(f"production artifacts changed during isolated acceptance: {changed[:5]}")


def run_acceptance(
    *,
    args: argparse.Namespace,
    paths: SmokePaths,
    inputs: ProductionInputs,
    audio_assets: Path,
    log_path: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    run_logged(build_match_command(inputs, paths), log_path=log_path, env=env)
    full_edl = paths.full_dir / "edl.json"
    full_source_map = paths.full_dir / "edl.source_map.json"
    full_match = validate_dynamic_outputs(
        edl_path=full_edl,
        qa_path=paths.full_dir / "edl.qa.json",
        beats_path=inputs.review_script,
        timings_path=inputs.beats_timing,
        episode_run_dirs=inputs.episode_run_dirs,
    )
    run_logged(
        build_postprocess_command(
            edl=full_edl,
            review_script=inputs.review_script,
            event_bank=inputs.event_bank,
            beats_timing=inputs.beats_timing,
            source_map=full_source_map,
            audio_assets=audio_assets,
            episode_run_dirs=inputs.episode_run_dirs,
            output_dir=paths.full_dir,
            seed=args.seed,
        ),
        log_path=log_path,
        env=env,
    )
    full_postprocess = validate_postprocess_outputs(
        plan_path=paths.full_dir / "edit_plan.json",
        qa_path=paths.full_dir / "edit_plan.qa.json",
        edl_path=full_edl,
        require_zoom=True,
        require_freeze=True,
        require_sfx=True,
    )

    excerpt_edl, _, _ = build_excerpt_artifacts(
        full_edl_path=full_edl,
        full_source_map_path=full_source_map,
        review_script_path=inputs.review_script,
        beats_timing_path=inputs.beats_timing,
        output_dir=paths.excerpt_dir,
        duration_s=float(args.excerpt_seconds),
    )
    excerpt_duration = excerpt_edl[-1].tl_end
    trim_voiceover(
        voiceover=inputs.voiceover,
        output=paths.excerpt_dir / "voiceover.mp3",
        duration_s=excerpt_duration,
        log_path=log_path,
        env=env,
    )
    run_logged(
        build_postprocess_command(
            edl=paths.excerpt_dir / "edl.json",
            review_script=paths.excerpt_dir / "series_review_script.json",
            event_bank=inputs.event_bank,
            beats_timing=paths.excerpt_dir / "beats_timing.json",
            source_map=paths.excerpt_dir / "edl.source_map.json",
            audio_assets=audio_assets,
            episode_run_dirs=inputs.episode_run_dirs,
            output_dir=paths.excerpt_dir,
            seed=args.seed,
        ),
        log_path=log_path,
        env=env,
    )
    require_freeze = int(full_postprocess["n_freeze"]) > 0
    require_sfx = int(full_postprocess["n_whoosh"]) + int(full_postprocess["n_impact"]) > 0
    acceptance_override: dict[str, Any] | None = None
    auto_excerpt_plan = EditPlan.model_validate(load_json(paths.excerpt_dir / "edit_plan.json"))
    if require_freeze and not any(edit.freeze_duration_s > 0 for edit in auto_excerpt_plan.placements):
        override_path = paths.excerpt_dir / "edit_overrides.acceptance.json"
        placement_index = write_acceptance_freeze_override(
            edl_path=paths.excerpt_dir / "edl.json",
            review_script_path=paths.excerpt_dir / "series_review_script.json",
            episode_run_dirs=inputs.episode_run_dirs,
            output_path=override_path,
        )
        run_logged(
            build_postprocess_command(
                edl=paths.excerpt_dir / "edl.json",
                review_script=paths.excerpt_dir / "series_review_script.json",
                event_bank=inputs.event_bank,
                beats_timing=paths.excerpt_dir / "beats_timing.json",
                source_map=paths.excerpt_dir / "edl.source_map.json",
                audio_assets=audio_assets,
                episode_run_dirs=inputs.episode_run_dirs,
                output_dir=paths.excerpt_dir,
                seed=args.seed,
                overrides=override_path,
                force=True,
            ),
            log_path=log_path,
            env=env,
        )
        acceptance_override = {
            "reason": "auto_first_90s_had_no_freeze_but_full_plan_proved_freeze_and_impact_capability",
            "placement_index": placement_index,
            "path": str(override_path),
        }
    excerpt_postprocess = validate_postprocess_outputs(
        plan_path=paths.excerpt_dir / "edit_plan.json",
        qa_path=paths.excerpt_dir / "edit_plan.qa.json",
        edl_path=paths.excerpt_dir / "edl.json",
        require_zoom=True,
        require_freeze=require_freeze,
        require_sfx=require_sfx,
    )
    run_logged(build_render_command(paths=paths, audio_assets=audio_assets), log_path=log_path, env=env)
    render = validate_render_output(
        paths=paths,
        duration_s=excerpt_duration,
        require_zoom=True,
        require_freeze=require_freeze,
        require_sfx=require_sfx,
    )
    return {
        "status": "passed",
        "season_run_dir": str(paths.season_run_dir),
        "audio_assets": str(audio_assets),
        "requested_excerpt_duration_s": float(args.excerpt_seconds),
        "excerpt_duration_s": excerpt_duration,
        "full_match": full_match,
        "full_postprocess": full_postprocess,
        "excerpt_postprocess": excerpt_postprocess,
        "acceptance_override": acceptance_override,
        "render": render,
        "production_artifacts_unchanged": True,
    }


def main() -> int:
    args = build_parser().parse_args()
    require_ffmpeg()
    if args.excerpt_seconds <= 0:
        raise RealMediaSmokeError("--excerpt-seconds must be greater than zero")
    paths = build_paths(args)
    audio_assets = resolve_repo_path(args.audio_assets)
    if audio_assets.name != "audio_assets.test.yaml":
        raise RealMediaSmokeError("real-media acceptance requires the non-production audio_assets.test.yaml manifest")
    validate_audio_assets(audio_assets)
    inputs = load_production_inputs(paths)
    paths.full_dir.mkdir(parents=True, exist_ok=True)
    paths.excerpt_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.work_dir / "commands.log"
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    for name in ("OPENAI_API_KEY", "VIVOO_API_KEY", "GENMAX_API_KEY"):
        env.pop(name, None)
    production_source_map = paths.production_final_dir / "edl.source_map.json"
    with production_artifact_guard(
        inputs,
        production_source_map,
        failure_report=paths.work_dir / "production-mutation.json",
    ):
        report = run_acceptance(
            args=args,
            paths=paths,
            inputs=inputs,
            audio_assets=audio_assets,
            log_path=log_path,
            env=env,
        )
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
