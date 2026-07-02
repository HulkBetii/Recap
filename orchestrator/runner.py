from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.media import require_ffmpeg
from common.schema import (
    BeatTiming,
    EdlPlacement,
    FilmMapMeta,
    FilmMapSegment,
    RenderMeta,
    ReviewBeat,
    ReviewMeta,
    Shot,
    ShotsMeta,
    TtsMeta,
    validate_beats_timing,
    validate_edl,
    validate_film_map,
    validate_review_script,
    validate_shots,
)
from orchestrator.config import add_option
from orchestrator.graph import RunPaths, STAGES
from orchestrator.summary import StageSummary

class OrchestratorError(RuntimeError):
    pass

@dataclass(frozen=True)
class StageSpec:
    name: str
    outputs: tuple[str, ...]
    meta: str | None

STAGE_SPECS: dict[str, StageSpec] = {
    "ingest": StageSpec("ingest", ("film_map", "film_map_meta"), "film_map_meta"),
    "review": StageSpec("review", ("review_script", "review_meta"), "review_meta"),
    "tts": StageSpec("tts", ("voiceover", "beats_timing", "tts_meta"), "tts_meta"),
    "shots": StageSpec("shots", ("shots", "shots_meta"), "shots_meta"),
    "match": StageSpec("match", ("edl", "edl_meta"), "edl_meta"),
    "render": StageSpec("render", ("recap", "render_meta"), "render_meta"),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def output_paths(paths: RunPaths, stage: str) -> list[Path]:
    return [getattr(paths, name) for name in STAGE_SPECS[stage].outputs]


def all_outputs_exist(paths: RunPaths, stage: str) -> bool:
    return all(path.is_file() for path in output_paths(paths, stage))


def validate_stage(paths: RunPaths, stage: str) -> None:
    try:
        if stage == "ingest":
            meta = FilmMapMeta.model_validate(load_json(paths.film_map_meta))
            validate_film_map([FilmMapSegment.model_validate(item) for item in load_json(paths.film_map)], duration=meta.duration)
        elif stage == "review":
            film_map = [FilmMapSegment.model_validate(item) for item in load_json(paths.film_map)]
            beats = [ReviewBeat.model_validate(item) for item in load_json(paths.review_script)]
            validate_review_script(beats, film_map)
            ReviewMeta.model_validate(load_json(paths.review_meta))
        elif stage == "tts":
            validate_beats_timing([BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)], pause_s=0.0)
            TtsMeta.model_validate(load_json(paths.tts_meta))
            if not paths.voiceover.is_file():
                raise ValueError("voiceover.mp3 is missing")
        elif stage == "shots":
            meta = ShotsMeta.model_validate(load_json(paths.shots_meta))
            validate_shots([Shot.model_validate(item) for item in load_json(paths.shots)], duration=meta.duration_s)
        elif stage == "match":
            timings = validate_beats_timing([BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)], pause_s=0.0)
            total_duration = timings[-1].tl_end if timings else None
            validate_edl([EdlPlacement.model_validate(item) for item in load_json(paths.edl)], total_duration=total_duration)
        elif stage == "render":
            RenderMeta.model_validate(load_json(paths.render_meta))
            if not paths.recap.is_file():
                raise ValueError("recap.mp4 is missing")
        else:
            raise ValueError(f"unknown stage: {stage}")
    except Exception as exc:  # noqa: BLE001 - rewrap stage context for CLI users
        raise OrchestratorError(f"{stage} output validation failed: {exc}") from exc


def outputs_valid(paths: RunPaths, stage: str) -> bool:
    if not all_outputs_exist(paths, stage):
        return False
    try:
        validate_stage(paths, stage)
        return True
    except OrchestratorError:
        return False


def stage_work(paths: RunPaths, stage: str) -> Path:
    return paths.work_dir / stage


def build_command(stage: str, paths: RunPaths, film: Path, config: dict[str, Any], force: bool, python_exe: str | None = None) -> list[str]:
    py = python_exe or sys.executable
    section = config.get(stage, {})
    command = [py, "-m", stage]
    if stage == "ingest":
        command += ["--input", str(film), "--output", str(paths.film_map)]
        for key in ("whisper_model", "gap_threshold", "max_vision_frames", "translate_model", "vision_model", "device", "asr_provider", "aligner", "transcript_input", "timecode_quality", "max_segment_s", "merge_gap_s", "openai_transcribe_model", "openai_chunk_s", "alignment_device", "transcript_correction", "glossary", "correction_model", "drop_non_korean_intro_s", "log_level"):
            add_option(command, key, section.get(key))
        if not section.get("vad_filter", True):
            command.append("--no-vad-filter")
    elif stage == "review":
        command += ["--film-map", str(paths.film_map), "--output", str(paths.review_script)]
        for key in ("target_ratio", "tts_cps", "min_coverage", "max_qa_iterations", "style_sample", "chatgpt_profile_dir", "log_level"):
            add_option(command, key, section.get(key))
        if section.get("headless"):
            command.append("--headless")
    elif stage == "tts":
        command += ["--review-script", str(paths.review_script), "--output-audio", str(paths.voiceover), "--output-timing", str(paths.beats_timing)]
        for key in ("voice_id", "provider_mode", "genmax_voice_id", "model", "speed", "inter_beat_pause", "concurrency", "cost_per_1k_chars", "log_level"):
            add_option(command, key, section.get(key))
        command += ["--film-meta", str(paths.film_map_meta)]
        if not section.get("normalize", True):
            command.append("--no-normalize")
    elif stage == "shots":
        command += ["--input", str(film), "--output", str(paths.shots), "--thumb-dir", str(paths.shots_dir)]
        for key in ("detector", "min_shot_len", "sample_frames", "face_detection", "min_brightness", "skip_intro", "skip_outro", "downscale", "log_level"):
            add_option(command, key, section.get(key))
    elif stage == "match":
        command += ["--review-script", str(paths.review_script), "--beats-timing", str(paths.beats_timing), "--shots", str(paths.shots), "--output", str(paths.edl)]
        for key in ("min_clip", "max_clip", "widen_margin", "max_widen", "seed", "w_motion", "w_face", "w_bright", "w_reuse", "log_level"):
            add_option(command, key, section.get(key))
        command.append("--allow-repeat" if section.get("allow_repeat", True) else "--no-allow-repeat")
        command.append("--allow-speedfit" if section.get("allow_speedfit", False) else "--no-allow-speedfit")
    elif stage == "render":
        command += ["--edl", str(paths.edl), "--voiceover", str(paths.voiceover), "--film", str(film), "--output", str(paths.recap)]
        for key in ("width", "height", "fps", "fit", "crf", "preset", "concurrency", "log_level"):
            add_option(command, key, section.get(key))
    else:
        raise OrchestratorError(f"unknown stage: {stage}")
    command += ["--work-dir", str(stage_work(paths, stage))]
    if force:
        command.append("--force")
    return command


def preflight(*, film: Path, selected: set[str], forced: set[str], paths: RunPaths, config: dict[str, Any], dry_run: bool = False) -> None:
    if not film.is_file():
        raise OrchestratorError(f"input film does not exist: {film}")
    if selected & {"ingest", "tts", "shots", "render"} and not dry_run:
        require_ffmpeg()
    will_run = {stage for stage in selected if stage in forced or not outputs_valid(paths, stage)}
    if "ingest" in will_run and not os.getenv("OPENAI_API_KEY") and not dry_run:
        raise OrchestratorError("OPENAI_API_KEY is required to run ingest")
    if "review" in will_run and not dry_run:
        profile = Path(str(config["review"].get("chatgpt_profile_dir"))).expanduser()
        if not profile.exists():
            raise OrchestratorError(f"ChatGPT profile dir does not exist for review: {profile}")
    if "tts" in will_run and not dry_run:
        tts_config = config["tts"]
        if not tts_config.get("voice_id"):
            raise OrchestratorError("tts.voice_id must be set in config")
        mode = tts_config.get("provider_mode", "auto")
        if mode in {"auto", "ai33"} and not os.getenv("VIVOO_API_KEY"):
            raise OrchestratorError("VIVOO_API_KEY is required for tts provider_mode auto/ai33")
        if mode in {"auto", "genmax"} and not os.getenv("GENMAX_API_KEY"):
            raise OrchestratorError("GENMAX_API_KEY is required for tts provider_mode auto/genmax")


def run_subprocess(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(command) + "\n")
        log_file.flush()
        result = subprocess.run(command, stdout=log_file, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode != 0:
        raise OrchestratorError(f"stage command failed with exit code {result.returncode}: {' '.join(command)}")


def run_stage(
    *,
    stage: str,
    paths: RunPaths,
    film: Path,
    config: dict[str, Any],
    force: bool,
    dry_run: bool,
    python_exe: str | None = None,
    executor: Callable[[list[str], Path], None] = run_subprocess,
) -> StageSummary:
    command = build_command(stage, paths, film, config, force, python_exe=python_exe)
    outputs = [str(path) for path in output_paths(paths, stage)]
    if not force and outputs_valid(paths, stage):
        return StageSummary(stage=stage, status="skipped", duration_s=0.0, command=command, outputs=outputs)
    if dry_run:
        return StageSummary(stage=stage, status="planned", duration_s=0.0, command=command, outputs=outputs)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    executor(command, paths.run_log)
    if stage == "review" and paths.review_meta.is_file() and not paths.review_meta_alias.exists():
        shutil.copyfile(paths.review_meta, paths.review_meta_alias)
    validate_stage(paths, stage)
    return StageSummary(stage=stage, status="ran", duration_s=round(time.perf_counter() - started, 3), command=command, outputs=outputs)


def ordered_selected(selected: set[str]) -> list[str]:
    return [stage for stage in STAGES if stage in selected]
