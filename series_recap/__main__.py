from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common.inputs import load_series_manifest
from common.schema import (
    BeatTiming,
    EdlPlacement,
    EdlSourceMap,
    RenderMeta,
    SeasonTargetPlan,
    SeriesChapter,
    SeriesComposerQa,
    SeriesManifest,
    SeriesManifestEpisode,
    SeriesReviewBeat,
    TtsMeta,
    validate_beats_timing,
    validate_edl,
    validate_series_review_script,
    write_json,
)
from orchestrator.config import ConfigError, add_option, load_config
from orchestrator.graph import build_paths as build_episode_paths
from orchestrator.runner import outputs_valid as episode_outputs_valid

EPISODE_KEY_RE = re.compile(r"(?:s(?P<season>\d{1,2})e(?P<episode>\d{1,3})|e(?P<episode_only>\d{1,3}))", re.IGNORECASE)

class SeriesRecapError(RuntimeError):
    pass

@dataclass(frozen=True)
class EpisodeSpec:
    episode_key: str
    episode_number: int | str | None
    title: str | None
    source_path: Path
    arc: str | None
    spoiler_limit_episode: int | str | None

@dataclass(frozen=True)
class SeriesPaths:
    root_dir: Path
    final_dir: Path
    config_dir: Path
    work_dir: Path
    log_path: Path
    summary: Path
    event_bank: Path
    series_arc_plan: Path
    series_composer_qa: Path
    series_review_script: Path
    series_review_meta: Path
    series_tts_script: Path
    series_chapters: Path
    youtube_chapters: Path
    voiceover: Path
    beats_timing: Path
    tts_meta: Path
    edl: Path
    source_map: Path
    edl_meta: Path
    edl_qa: Path
    output_video: Path
    render_meta: Path

@dataclass(frozen=True)
class StepSummary:
    stage: str
    status: str
    duration_s: float
    command: list[str]
    outputs: list[str]

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "status": self.status,
            "duration_s": self.duration_s,
            "command": self.command,
            "outputs": self.outputs,
        }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multi-episode anime recap: episodes -> one series_recap.mp4")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--config", default=Path("config.anime.series.yaml"), type=Path)
    parser.add_argument("--episodes", default=None, help="Comma list or range, e.g. s03e01,s03e03 or 1-3")
    parser.add_argument("--run-dir", default=None, type=Path, help="Root run directory; defaults to runs/<series_id>")
    parser.add_argument("--python", default=None, help="Python executable for subprocess stages")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Force episode and final stages")
    parser.add_argument("--force-final", action="store_true", help="Force only composer/TTS/match/render")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def numeric_episode(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None

def normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "episode"

def season_prefix(season: int | str | None) -> str | None:
    number = numeric_episode(season)
    return f"s{number:02d}" if number is not None else None

def default_episode_key(manifest: SeriesManifest, episode: SeriesManifestEpisode, source_path: Path) -> str:
    if episode.episode_key:
        return episode.episode_key
    episode_number = numeric_episode(episode.episode_number)
    prefix = season_prefix(manifest.season)
    if episode_number is not None and prefix is not None:
        return f"{prefix}e{episode_number:02d}"
    if episode_number is not None:
        return f"e{episode_number:02d}"
    match = EPISODE_KEY_RE.search(source_path.stem)
    if match:
        parsed_episode = match.group("episode") or match.group("episode_only")
        parsed_season = match.group("season")
        if parsed_episode and parsed_season:
            return f"s{int(parsed_season):02d}e{int(parsed_episode):02d}"
        if parsed_episode:
            return f"e{int(parsed_episode):02d}"
    return normalize_key(source_path.stem)

def top_level_episode(manifest: SeriesManifest) -> SeriesManifestEpisode | None:
    if any((manifest.episode_key, manifest.episode_number, manifest.source_path, manifest.title, manifest.arc)):
        return SeriesManifestEpisode(
            episode_key=manifest.episode_key,
            episode_number=manifest.episode_number,
            title=manifest.title,
            source_path=manifest.source_path,
            arc=manifest.arc,
            spoiler_limit_episode=manifest.spoiler_limit_episode,
        )
    return None

def resolve_source_path(raw_path: str | None, manifest_path: Path) -> Path:
    if not raw_path or not raw_path.strip():
        raise SeriesRecapError("every manifest episode requires source_path")
    source_path = Path(raw_path).expanduser()
    if not source_path.is_absolute():
        source_path = manifest_path.parent / source_path
    return source_path.resolve()

def manifest_episode_specs(manifest_path: Path) -> tuple[SeriesManifest, list[EpisodeSpec]]:
    manifest = load_series_manifest(manifest_path)
    raw_episodes = list(manifest.episodes)
    top_level = top_level_episode(manifest)
    if top_level is not None:
        raw_episodes.insert(0, top_level)
    if not raw_episodes:
        raise SeriesRecapError("series manifest has no episodes")

    specs: list[EpisodeSpec] = []
    for episode in raw_episodes:
        source_path = resolve_source_path(episode.source_path, manifest_path)
        episode_key = default_episode_key(manifest, episode, source_path)
        episode_number = episode.episode_number
        if episode_number is None:
            episode_number = numeric_episode(episode_key)
        specs.append(
            EpisodeSpec(
                episode_key=episode_key,
                episode_number=episode_number,
                title=episode.title,
                source_path=source_path,
                arc=episode.arc,
                spoiler_limit_episode=episode.spoiler_limit_episode,
            )
        )

    keys = [spec.episode_key for spec in specs]
    duplicate_keys = sorted(key for key in set(keys) if keys.count(key) > 1)
    if duplicate_keys:
        raise SeriesRecapError(f"duplicate episode_key in manifest: {duplicate_keys}")
    sources = [str(spec.source_path).lower() for spec in specs]
    duplicate_sources = sorted(source for source in set(sources) if sources.count(source) > 1)
    if duplicate_sources:
        raise SeriesRecapError(f"duplicate source_path in manifest: {duplicate_sources}")
    return manifest, specs

def selector_episode_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    match = EPISODE_KEY_RE.fullmatch(token.lower())
    if not match:
        return None
    episode = match.group("episode") or match.group("episode_only")
    return int(episode) if episode else None

def select_one_episode(token: str, specs: list[EpisodeSpec]) -> EpisodeSpec:
    normalized = token.strip().lower()
    for spec in specs:
        if spec.episode_key.lower() == normalized:
            return spec
    number = selector_episode_number(normalized)
    if number is not None:
        matches = [spec for spec in specs if numeric_episode(spec.episode_number) == number]
        if len(matches) == 1:
            return matches[0]
    raise SeriesRecapError(f"episode selector did not match manifest: {token}")

def select_episodes(specs: list[EpisodeSpec], selector: str | None) -> list[EpisodeSpec]:
    if selector is None or not selector.strip():
        return specs
    selected: list[EpisodeSpec] = []
    for token in [part.strip() for part in selector.split(",") if part.strip()]:
        if "-" in token:
            start_token, end_token = [part.strip() for part in token.split("-", 1)]
            start = select_one_episode(start_token, specs)
            end = select_one_episode(end_token, specs)
            start_index = specs.index(start)
            end_index = specs.index(end)
            if start_index > end_index:
                raise SeriesRecapError(f"episode range start comes after end: {token}")
            selected.extend(specs[start_index : end_index + 1])
        else:
            selected.append(select_one_episode(token, specs))
    deduped: list[EpisodeSpec] = []
    seen: set[str] = set()
    for spec in selected:
        if spec.episode_key not in seen:
            deduped.append(spec)
            seen.add(spec.episode_key)
    return deduped

def build_paths(root_dir: Path) -> SeriesPaths:
    final_dir = root_dir / "series_recap"
    work_dir = final_dir / "work"
    return SeriesPaths(
        root_dir=root_dir,
        final_dir=final_dir,
        config_dir=work_dir / "episode_configs",
        work_dir=work_dir,
        log_path=final_dir / "series_recap.log",
        summary=final_dir / "summary.json",
        event_bank=final_dir / "series_event_bank.json",
        series_arc_plan=final_dir / "series_arc_plan.json",
        series_composer_qa=final_dir / "series_composer.qa.json",
        series_review_script=final_dir / "series_review_script.json",
        series_review_meta=final_dir / "series_review_script.meta.json",
        series_tts_script=final_dir / "series_tts_script.json",
        series_chapters=final_dir / "series_chapters.json",
        youtube_chapters=final_dir / "youtube_chapters.txt",
        voiceover=final_dir / "voiceover.mp3",
        beats_timing=final_dir / "beats_timing.json",
        tts_meta=final_dir / "tts_meta.json",
        edl=final_dir / "edl.json",
        source_map=final_dir / "edl.source_map.json",
        edl_meta=final_dir / "edl.meta.json",
        edl_qa=final_dir / "edl.qa.json",
        output_video=final_dir / "series_recap.mp4",
        render_meta=final_dir / "render.meta.json",
    )

def episode_config_for(
    *,
    base_config: dict[str, Any],
    manifest_path: Path,
    spec: EpisodeSpec,
    series_memory_dir: Path,
) -> dict[str, Any]:
    config = deepcopy(base_config)
    orchestrator = config.setdefault("orchestrator", {})
    orchestrator["series_manifest"] = str(manifest_path)
    orchestrator["episode_key"] = spec.episode_key
    orchestrator["episode_number"] = spec.episode_number
    orchestrator["series_memory_dir"] = str(series_memory_dir)
    if orchestrator.get("recap_mode") in {None, "off"}:
        orchestrator["recap_mode"] = "auto"
    preflight = config.setdefault("preflight", {})
    if not preflight.get("manual_ranges"):
        manual_ranges = discover_episode_sidecar(manifest_path, spec.episode_key, "manual_ranges")
        if manual_ranges is not None:
            preflight["manual_ranges"] = str(manual_ranges)
    return config

def discover_episode_sidecar(manifest_path: Path, episode_key: str, stem: str) -> Path | None:
    base_dir = manifest_path.parent
    candidates = [
        base_dir / f"{stem}.{episode_key}.yaml",
        base_dir / f"{stem}.{episode_key}.yml",
        base_dir / f"{stem}.{episode_key}.json",
        base_dir / f"{episode_key}.{stem}.yaml",
        base_dir / f"{episode_key}.{stem}.yml",
        base_dir / f"{episode_key}.{stem}.json",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None

def write_episode_config(
    *,
    config: dict[str, Any],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path

def python_executable(args: argparse.Namespace, config: dict[str, Any]) -> str:
    return args.python or config.get("orchestrator", {}).get("python") or sys.executable

def episode_commands(
    *,
    py: str,
    spec: EpisodeSpec,
    episode_run_dir: Path,
    config_path: Path,
    force: bool,
) -> list[tuple[str, list[str], list[Path]]]:
    base = [
        py,
        "run.py",
        "--input",
        str(spec.source_path),
        "--run-dir",
        str(episode_run_dir),
        "--config",
        str(config_path),
    ]
    planner = base + ["--to", "episode_planner"]
    shots = base + ["--only", "shots"]
    if force:
        planner.append("--force")
        shots.append("--force")
    return [
        ("episode_planner", planner, [episode_run_dir / "episode_meta.json", episode_run_dir / "episode_memory.json"]),
        ("episode_shots", shots, [episode_run_dir / "shots.json", episode_run_dir / "shots.meta.json"]),
    ]

def mode_target_ratio_args(section: dict[str, Any]) -> list[str]:
    ratios = section.get("mode_target_ratios") or {}
    args: list[str] = []
    if isinstance(ratios, dict):
        for mode in ("full", "quick", "merge", "skip"):
            if mode in ratios:
                args.extend(["--mode-target-ratio", f"{mode}={ratios[mode]}"])
    return args

def composer_command(
    *,
    py: str,
    manifest_path: Path,
    episode_run_dirs: dict[str, Path],
    paths: SeriesPaths,
    config: dict[str, Any],
    force: bool,
) -> list[str]:
    section = config.get("series_recap", {})
    command = [
        py,
        "-m",
        "series_composer",
        "--manifest",
        str(manifest_path),
    ]
    for episode_key, run_dir in episode_run_dirs.items():
        command.extend(["--episode-run-dir", f"{episode_key}={run_dir}"])
    command.extend(
        [
            "--output-event-bank",
            str(paths.event_bank),
            "--output",
            str(paths.series_review_script),
            "--output-tts-script",
            str(paths.series_tts_script),
            "--output-chapters",
            str(paths.series_chapters),
            "--output-arc-plan",
            str(paths.series_arc_plan),
            "--output-qa",
            str(paths.series_composer_qa),
            "--output-meta",
            str(paths.series_review_meta),
            "--work-dir",
            str(paths.work_dir / "series_composer"),
        ]
    )
    add_option(command, "format", section.get("format"))
    for key in (
        "detail_level",
        "tts_cps",
        "target_total_min_s",
        "target_total_max_s",
        "target_total_hard_cap_s",
        "episode_min_s",
        "episode_normal_s",
        "episode_high_s",
        "arc_size",
        "chatgpt_profile_dir",
        "reply_timeout_s",
        "playwright_max_attempts",
        "playwright_recovery_timeout_s",
        "qa_max_revisions",
        "log_level",
    ):
        add_option(command, key, section.get(key))
    command.extend(mode_target_ratio_args(section))
    if section.get("headless"):
        command.append("--headless")
    if force:
        command.append("--force")
    return command

def tts_command(*, py: str, paths: SeriesPaths, config: dict[str, Any], force: bool) -> list[str]:
    section = config.get("tts", {})
    command = [
        py,
        "-m",
        "tts",
        "--review-script",
        str(paths.series_tts_script),
        "--output-audio",
        str(paths.voiceover),
        "--output-timing",
        str(paths.beats_timing),
    ]
    for key in (
        "voice_id",
        "provider_mode",
        "genmax_voice_id",
        "model",
        "openai_model",
        "openai_voice",
        "speed",
        "inter_beat_pause",
        "concurrency",
        "cost_per_1k_chars",
        "log_level",
    ):
        add_option(command, key, section.get(key))
    add_option(command, "tts_text_normalization", section.get("text_normalization"))
    add_option(command, "tts_pronunciation_lexicon", section.get("pronunciation_lexicon"))
    add_option(command, "tts_normalized_script_output", section.get("normalized_script_output"))
    add_option(command, "tts_normalization_report", section.get("normalization_report"))
    add_option(command, "pronunciation_qa_output", section.get("pronunciation_qa_output"))
    add_option(command, "pronunciation_suggest_backend", section.get("pronunciation_suggest_backend"))
    add_option(command, "lexicon_candidates_output", section.get("lexicon_candidates_output"))
    command.append("--pronunciation-qa" if section.get("pronunciation_qa", True) else "--no-pronunciation-qa")
    if not section.get("normalize", True):
        command.append("--no-normalize")
    command.extend(["--work-dir", str(paths.work_dir / "tts")])
    if force:
        command.append("--force")
    return command

def series_match_command(
    *,
    py: str,
    episode_run_dirs: dict[str, Path],
    paths: SeriesPaths,
    config: dict[str, Any],
) -> list[str]:
    section = config.get("series_recap", {})
    command = [
        py,
        "-m",
        "series_match",
        "--series-review-script",
        str(paths.series_review_script),
        "--beats-timing",
        str(paths.beats_timing),
    ]
    for episode_key, run_dir in episode_run_dirs.items():
        command.extend(["--episode-run-dir", f"{episode_key}={run_dir}"])
    command.extend(
        [
            "--output",
            str(paths.edl),
            "--output-source-map",
            str(paths.source_map),
            "--output-qa",
            str(paths.edl_qa),
            "--work-dir",
            str(paths.work_dir / "series_match"),
        ]
    )
    for key in ("min_clip", "max_clip", "min_visual_clip", "log_level"):
        add_option(command, key, section.get(key))
    return command

def render_command(*, py: str, paths: SeriesPaths, config: dict[str, Any], force: bool) -> list[str]:
    section = config.get("render", {})
    command = [
        py,
        "-m",
        "render",
        "--edl",
        str(paths.edl),
        "--voiceover",
        str(paths.voiceover),
        "--source-map",
        str(paths.source_map),
        "--output",
        str(paths.output_video),
    ]
    for key in ("width", "height", "fps", "fit", "crf", "preset", "concurrency", "audio_delay_s", "log_level"):
        add_option(command, key, section.get(key))
    command.extend(["--work-dir", str(paths.work_dir / "render")])
    if force:
        command.append("--force")
    return command

def run_subprocess(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(command) + "\n")
        log_file.flush()
        result = subprocess.run(command, stdout=log_file, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode != 0:
        raise SeriesRecapError(f"stage command failed with exit code {result.returncode}: {' '.join(command)}")

def files_exist(paths: list[Path]) -> bool:
    return all(path.is_file() for path in paths)

def episode_stage_valid(stage_name: str, run_dir: Path, film: Path, config: dict[str, Any]) -> bool:
    run_paths = build_episode_paths(run_dir)
    if stage_name == "episode_planner":
        stages = ["ingest", "storymap", "episode_planner"]
        if config.get("preflight", {}).get("enabled", True):
            stages.insert(0, "preflight")
    elif stage_name == "episode_shots":
        stages = ["shots"]
        if config.get("preflight", {}).get("enabled", True):
            stages.insert(0, "preflight")
    else:
        raise SeriesRecapError(f"unknown episode stage alias: {stage_name}")
    return all(episode_outputs_valid(run_paths, stage, film=film, config=config) for stage in stages)

def composer_outputs_valid(paths: SeriesPaths) -> bool:
    if not files_exist(
        [
            paths.event_bank,
            paths.series_arc_plan,
            paths.series_composer_qa,
            paths.series_review_script,
            paths.series_tts_script,
            paths.series_chapters,
            paths.series_review_meta,
        ]
    ):
        return False
    beats = [SeriesReviewBeat.model_validate(item) for item in load_json(paths.series_review_script)]
    validate_series_review_script(beats)
    [SeriesChapter.model_validate(item) for item in load_json(paths.series_chapters)]
    SeasonTargetPlan.model_validate(load_json(paths.series_arc_plan))
    SeriesComposerQa.model_validate(load_json(paths.series_composer_qa))
    return True

def tts_outputs_valid(paths: SeriesPaths) -> bool:
    if not files_exist([paths.voiceover, paths.beats_timing, paths.tts_meta]):
        return False
    meta = TtsMeta.model_validate(load_json(paths.tts_meta))
    timings = [BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)]
    validate_beats_timing(timings, pause_s=meta.inter_beat_pause_s)
    return True

def youtube_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def write_youtube_chapters(*, chapters_path: Path, beats_timing_path: Path, output_path: Path) -> Path:
    chapters = [SeriesChapter.model_validate(item) for item in load_json(chapters_path)]
    timings = [BeatTiming.model_validate(item) for item in load_json(beats_timing_path)]
    timings_by_beat = {timing.beat_id: timing for timing in timings}
    lines: list[str] = []
    seen_beat_ids: set[int] = set()
    for chapter in sorted(chapters, key=lambda item: item.start_beat_id):
        if chapter.start_beat_id in seen_beat_ids:
            continue
        timing = timings_by_beat.get(chapter.start_beat_id)
        if timing is None:
            raise SeriesRecapError(f"chapter references missing beat timing: {chapter.start_beat_id}")
        lines.append(f"{youtube_timestamp(timing.tl_start)} {chapter.title}")
        seen_beat_ids.add(chapter.start_beat_id)
    if lines and not lines[0].startswith("00:00 "):
        lines.insert(0, "00:00 Mo dau")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path

def youtube_chapters_outputs_valid(paths: SeriesPaths) -> bool:
    if not files_exist([paths.series_chapters, paths.beats_timing, paths.youtube_chapters]):
        return False
    return bool(paths.youtube_chapters.read_text(encoding="utf-8").strip())

def run_youtube_chapters_step(
    *,
    paths: SeriesPaths,
    force: bool,
    dry_run: bool,
) -> StepSummary:
    command = ["internal", "youtube_chapters"]
    outputs = [str(paths.youtube_chapters)]
    if not force:
        try:
            if youtube_chapters_outputs_valid(paths):
                return StepSummary(
                    stage="youtube_chapters",
                    status="skipped",
                    duration_s=0.0,
                    command=command,
                    outputs=outputs,
                )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if dry_run:
        return StepSummary(stage="youtube_chapters", status="planned", duration_s=0.0, command=command, outputs=outputs)
    started = time.perf_counter()
    write_youtube_chapters(
        chapters_path=paths.series_chapters,
        beats_timing_path=paths.beats_timing,
        output_path=paths.youtube_chapters,
    )
    return StepSummary(
        stage="youtube_chapters",
        status="ran",
        duration_s=round(time.perf_counter() - started, 3),
        command=command,
        outputs=outputs,
    )

def series_match_outputs_valid(paths: SeriesPaths) -> bool:
    if not files_exist([paths.edl, paths.source_map, paths.edl_meta, paths.edl_qa]):
        return False
    timings = [BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)] if paths.beats_timing.is_file() else []
    total_duration = timings[-1].tl_end if timings else None
    validate_edl([EdlPlacement.model_validate(item) for item in load_json(paths.edl)], total_duration=total_duration)
    EdlSourceMap.model_validate(load_json(paths.source_map))
    return True

def render_outputs_valid(paths: SeriesPaths) -> bool:
    if not files_exist([paths.output_video, paths.render_meta]):
        return False
    RenderMeta.model_validate(load_json(paths.render_meta))
    return True

def run_step(
    *,
    stage: str,
    command: list[str],
    outputs: list[Path],
    valid: Callable[[], bool],
    log_path: Path,
    force: bool,
    dry_run: bool,
    executor: Callable[[list[str], Path], None],
) -> StepSummary:
    output_text = [str(path) for path in outputs]
    if not force:
        try:
            if valid():
                return StepSummary(stage=stage, status="skipped", duration_s=0.0, command=command, outputs=output_text)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if dry_run:
        return StepSummary(stage=stage, status="planned", duration_s=0.0, command=command, outputs=output_text)
    started = time.perf_counter()
    executor(command, log_path)
    try:
        if not valid():
            raise SeriesRecapError(f"{stage} outputs failed validation")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SeriesRecapError(f"{stage} outputs failed validation: {exc}") from exc
    return StepSummary(
        stage=stage,
        status="ran",
        duration_s=round(time.perf_counter() - started, 3),
        command=command,
        outputs=output_text,
    )

def print_plan(summaries: list[StepSummary]) -> None:
    for summary in summaries:
        print(f"[{summary.status}] {summary.stage}")
        print("  command: " + " ".join(summary.command))
        for output in summary.outputs:
            print(f"  output: {output}")

def run_series_recap(
    args: argparse.Namespace,
    executor: Callable[[list[str], Path], None] | None = None,
) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    manifest, all_specs = manifest_episode_specs(manifest_path)
    specs = select_episodes(all_specs, args.episodes)
    if not specs:
        raise SeriesRecapError("no episodes selected")
    config = load_config(args.config.expanduser().resolve() if args.config else None)
    if args.log_level is not None:
        config.setdefault("series_recap", {})["log_level"] = args.log_level
    root_dir = (args.run_dir.expanduser().resolve() if args.run_dir else (Path("runs") / manifest.series_id).resolve())
    paths = build_paths(root_dir)
    py = python_executable(args, config)
    runner = executor or run_subprocess
    force_final = bool(args.force or args.force_final)
    if not args.dry_run:
        paths.final_dir.mkdir(parents=True, exist_ok=True)
        paths.work_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        for spec in specs:
            if not spec.source_path.is_file():
                raise SeriesRecapError(f"episode source file does not exist: {spec.source_path}")
        if not config.get("tts", {}).get("voice_id"):
            raise SeriesRecapError("tts.voice_id must be set in config before final series TTS")

    episode_run_dirs: dict[str, Path] = {}
    summaries: list[StepSummary] = []
    series_memory_dir = root_dir / "series_memory"

    for spec in specs:
        episode_run_dir = root_dir / spec.episode_key
        episode_run_dirs[spec.episode_key] = episode_run_dir
        episode_config = episode_config_for(
            base_config=config,
            manifest_path=manifest_path,
            spec=spec,
            series_memory_dir=series_memory_dir,
        )
        config_path = paths.config_dir / f"{spec.episode_key}.json"
        if not args.dry_run:
            write_episode_config(
                config=episode_config,
                output_path=config_path,
            )
        commands = episode_commands(
            py=py,
            spec=spec,
            episode_run_dir=episode_run_dir,
            config_path=config_path,
            force=args.force,
        )
        planner_stage, planner_command, planner_outputs = commands[0]
        planner_summary = run_step(
            stage=f"{spec.episode_key}:{planner_stage}",
            command=planner_command,
            outputs=planner_outputs,
            valid=lambda stage_name=planner_stage, run_dir=episode_run_dir, film=spec.source_path, stage_config=episode_config: episode_stage_valid(
                stage_name,
                run_dir,
                film,
                stage_config,
            ),
            log_path=paths.log_path,
            force=args.force,
            dry_run=args.dry_run,
            executor=runner,
        )
        summaries.append(planner_summary)

        shots_stage, shots_command, shots_outputs = commands[1]
        force_shots = args.force or planner_summary.status == "ran"
        if force_shots and "--force" not in shots_command:
            shots_command = [*shots_command, "--force"]
        summaries.append(
            run_step(
                stage=f"{spec.episode_key}:{shots_stage}",
                command=shots_command,
                outputs=shots_outputs,
                valid=lambda stage_name=shots_stage, run_dir=episode_run_dir, film=spec.source_path, stage_config=episode_config: episode_stage_valid(
                    stage_name,
                    run_dir,
                    film,
                    stage_config,
                ),
                log_path=paths.log_path,
                force=force_shots,
                dry_run=args.dry_run,
                executor=runner,
            )
        )

    composer = run_step(
        stage="series_composer",
        command=composer_command(
            py=py,
            manifest_path=manifest_path,
            episode_run_dirs=episode_run_dirs,
            paths=paths,
            config=config,
            force=force_final,
        ),
        outputs=[
            paths.event_bank,
            paths.series_arc_plan,
            paths.series_composer_qa,
            paths.series_review_script,
            paths.series_review_meta,
            paths.series_tts_script,
            paths.series_chapters,
        ],
        valid=lambda: composer_outputs_valid(paths),
        log_path=paths.log_path,
        force=force_final,
        dry_run=args.dry_run,
        executor=runner,
    )
    summaries.append(composer)
    downstream_force = force_final or composer.status == "ran"

    tts = run_step(
        stage="tts",
        command=tts_command(py=py, paths=paths, config=config, force=downstream_force),
        outputs=[paths.voiceover, paths.beats_timing, paths.tts_meta],
        valid=lambda: tts_outputs_valid(paths),
        log_path=paths.log_path,
        force=downstream_force,
        dry_run=args.dry_run,
        executor=runner,
    )
    summaries.append(tts)
    downstream_force = downstream_force or tts.status == "ran"

    youtube_chapters = run_youtube_chapters_step(
        paths=paths,
        force=downstream_force,
        dry_run=args.dry_run,
    )
    summaries.append(youtube_chapters)

    match = run_step(
        stage="series_match",
        command=series_match_command(py=py, episode_run_dirs=episode_run_dirs, paths=paths, config=config),
        outputs=[paths.edl, paths.source_map, paths.edl_meta, paths.edl_qa],
        valid=lambda: series_match_outputs_valid(paths),
        log_path=paths.log_path,
        force=downstream_force,
        dry_run=args.dry_run,
        executor=runner,
    )
    summaries.append(match)
    downstream_force = downstream_force or match.status == "ran"

    summaries.append(
        run_step(
            stage="render",
            command=render_command(py=py, paths=paths, config=config, force=downstream_force),
            outputs=[paths.output_video, paths.render_meta],
            valid=lambda: render_outputs_valid(paths),
            log_path=paths.log_path,
            force=downstream_force,
            dry_run=args.dry_run,
            executor=runner,
        )
    )

    summary = {
        "series_id": manifest.series_id,
        "series_title": manifest.series_title,
        "episode_keys": [spec.episode_key for spec in specs],
        "output": str(paths.output_video),
        "youtube_chapters": str(paths.youtube_chapters),
        "series_arc_plan": str(paths.series_arc_plan),
        "series_composer_qa": str(paths.series_composer_qa),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stages": [item.to_json() for item in summaries],
    }
    if args.dry_run:
        print_plan(summaries)
    else:
        write_json(paths.summary, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_series_recap(args)
    except (SeriesRecapError, ConfigError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"series_recap: error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
