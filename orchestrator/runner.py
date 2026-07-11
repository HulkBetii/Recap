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
    EdlMeta,
    EdlPlacement,
    FilmMapMeta,
    FilmMapSegment,
    RenderMeta,
    VideoProfile,
    StoryMapMeta,
    StorySection,
    ReviewBeat,
    ReviewMeta,
    ReviewIntent,
    Shot,
    ShotVisualIndexFile,
    ShotsMeta,
    TtsMeta,
    validate_beats_timing,
    validate_edl,
    validate_film_map,
    validate_review_script,
    validate_review_intents,
    validate_shots,
    validate_shot_visual_index,
    validate_story_map,
)
from orchestrator.config import add_option
from orchestrator.cost_policy import CostPolicy, disallowed_openai_stages
from orchestrator.graph import RunPaths, STAGES
from orchestrator.summary import StageSummary
from visual_index.integrity import metadata_is_current, validate_visual_index_artifacts, visual_index_config_hash
from match.version import MATCH_ALGORITHM_VERSION

class OrchestratorError(RuntimeError):
    pass

@dataclass(frozen=True)
class StageSpec:
    name: str
    outputs: tuple[str, ...]
    meta: str | None

STAGE_SPECS: dict[str, StageSpec] = {
    "preflight": StageSpec("preflight", ("video_profile",), "video_profile"),
    "ingest": StageSpec("ingest", ("film_map", "film_map_meta"), "film_map_meta"),
    "storymap": StageSpec("storymap", ("story_map", "story_map_meta", "story_map_qa"), "story_map_meta"),
    "review": StageSpec("review", ("review_script", "review_meta"), "review_meta"),
    "tts": StageSpec("tts", ("voiceover", "beats_timing", "tts_meta"), "tts_meta"),
    "shots": StageSpec("shots", ("shots", "shots_meta"), "shots_meta"),
    "visual_index": StageSpec("visual_index", ("shot_visual_index",), "shot_visual_index"),
    "match": StageSpec("match", ("edl", "edl_meta", "edl_qa", "edl_sync_qa", "edl_visual_qa", "edl_review_html"), "edl_meta"),
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
        if stage == "preflight":
            VideoProfile.model_validate(load_json(paths.video_profile))
        elif stage == "ingest":
            meta = FilmMapMeta.model_validate(load_json(paths.film_map_meta))
            validate_film_map([FilmMapSegment.model_validate(item) for item in load_json(paths.film_map)], duration=meta.duration)
        elif stage == "storymap":
            meta = StoryMapMeta.model_validate(load_json(paths.story_map_meta))
            validate_story_map([StorySection.model_validate(item) for item in load_json(paths.story_map)], duration=meta.duration_s)
        elif stage == "review":
            film_map = [FilmMapSegment.model_validate(item) for item in load_json(paths.film_map)]
            beats = [ReviewBeat.model_validate(item) for item in load_json(paths.review_script)]
            validate_review_script(beats, film_map)
            ReviewMeta.model_validate(load_json(paths.review_meta))
            if paths.review_intent.is_file():
                validate_review_intents([ReviewIntent.model_validate(item) for item in load_json(paths.review_intent)], beats)
        elif stage == "tts":
            tts_meta = TtsMeta.model_validate(load_json(paths.tts_meta))
            validate_beats_timing([BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)], pause_s=tts_meta.inter_beat_pause_s)
            if not paths.voiceover.is_file():
                raise ValueError("voiceover.mp3 is missing")
        elif stage == "shots":
            meta = ShotsMeta.model_validate(load_json(paths.shots_meta))
            validate_shots([Shot.model_validate(item) for item in load_json(paths.shots)], duration=meta.duration_s)
        elif stage == "visual_index":
            shots = [Shot.model_validate(item) for item in load_json(paths.shots)] if paths.shots.is_file() else None
            index = validate_shot_visual_index(ShotVisualIndexFile.model_validate(load_json(paths.shot_visual_index)), shots)
            validate_visual_index_artifacts(
                paths.shot_visual_index,
                index,
                shots,
                require_frames=True,
                require_calibration=True,
            )
        elif stage == "match":
            pause_s = 0.0
            if paths.tts_meta.is_file():
                pause_s = TtsMeta.model_validate(load_json(paths.tts_meta)).inter_beat_pause_s
            timings = validate_beats_timing([BeatTiming.model_validate(item) for item in load_json(paths.beats_timing)], pause_s=pause_s)
            total_duration = timings[-1].tl_end if timings else None
            validate_edl([EdlPlacement.model_validate(item) for item in load_json(paths.edl)], total_duration=total_duration)
            match_meta = EdlMeta.model_validate(load_json(paths.edl_meta))
            if match_meta.algorithm_version != MATCH_ALGORITHM_VERSION:
                raise ValueError(
                    f"match algorithm version is stale: {match_meta.algorithm_version} != {MATCH_ALGORITHM_VERSION}"
                )
        elif stage == "render":
            RenderMeta.model_validate(load_json(paths.render_meta))
            if not paths.recap.is_file():
                raise ValueError("recap.mp4 is missing")
        else:
            raise ValueError(f"unknown stage: {stage}")
    except Exception as exc:  # noqa: BLE001 - rewrap stage context for CLI users
        raise OrchestratorError(f"{stage} output validation failed: {exc}") from exc


def outputs_valid(paths: RunPaths, stage: str, *, film: Path | None = None, config: dict[str, Any] | None = None) -> bool:
    if not all_outputs_exist(paths, stage):
        return False
    try:
        validate_stage(paths, stage)
        if stage == "visual_index" and film is not None and config is not None:
            section = config.get("visual_index", {})
            config_hash = visual_index_config_hash(
                film_path=film,
                shots_path=paths.shots,
                embedding_mode=str(section.get("embedding_mode", "siglip2")),
                embedding_model=str(section.get("embedding_model", "google/siglip2-base-patch16-384")),
                keyframes_per_shot=int(section.get("keyframes_per_shot", 2)),
                frame_sampling=str(section.get("frame_sampling", "per-frame")),
            )
            index = ShotVisualIndexFile.model_validate(load_json(paths.shot_visual_index))
            if not metadata_is_current(index, film_path=film, shots_path=paths.shots, config_hash=config_hash):
                return False
        return True
    except OrchestratorError:
        return False


def stage_work(paths: RunPaths, stage: str) -> Path:
    return paths.work_dir / stage


def build_command(stage: str, paths: RunPaths, film: Path, config: dict[str, Any], force: bool, python_exe: str | None = None) -> list[str]:
    py = python_exe or sys.executable
    section = config.get(stage, {})
    command = [py, "-m", stage]
    if stage == "preflight":
        command += ["--input", str(film), "--output", str(paths.video_profile)]
        section = config.get("preflight", {})
        if section.get("enabled", True):
            for key in ("max_intro_s", "sample_every_s", "classifier", "confidence_threshold", "uncertain_threshold", "log_level"):
                add_option(command, key, section.get(key))
        else:
            command += ["--classifier", "heuristic"]
    elif stage == "ingest":
        command += ["--input", str(film), "--output", str(paths.film_map)]
        for key in ("whisper_model", "gap_threshold", "max_vision_frames", "max_visual_gap_s", "translate_model", "source_language", "translate_mode", "vision_model", "device", "asr_provider", "aligner", "transcript_input", "timecode_quality", "max_segment_s", "merge_gap_s", "openai_transcribe_model", "openai_chunk_s", "alignment_device", "transcript_correction", "glossary", "correction_model", "drop_non_korean_intro_s", "drop_visual_before_s", "log_level"):
            add_option(command, key, section.get(key))
        # GĐ1 CLI does not currently accept video_profile directly; downstream
        # visual stages consume it when preflight is enabled.
        if not section.get("vad_filter", True):
            command.append("--no-vad-filter")
    elif stage == "storymap":
        command += ["--film-map", str(paths.film_map), "--output", str(paths.story_map), "--output-qa", str(paths.story_map_qa)]
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file():
            command += ["--video-profile", str(paths.video_profile)]
        for key in ("content_type", "target_story_sections", "log_level"):
            add_option(command, key, section.get(key))
    elif stage == "review":
        command += ["--film-map", str(paths.film_map), "--output", str(paths.review_script)]
        story_map_setting = section.get("story_map", "auto")
        if story_map_setting == "auto" and paths.story_map.is_file():
            command += ["--story-map", str(paths.story_map)]
        elif story_map_setting not in {None, "auto"}:
            command += ["--story-map", str(story_map_setting)]
        review_intent_output = section.get("review_intent_output")
        command += ["--review-intent-output", str(review_intent_output or paths.review_intent)]
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.exists():
            command += ["--video-profile", str(paths.video_profile)]
        for key in ("target_ratio", "tts_cps", "min_coverage", "max_qa_iterations", "max_qa_rewrites_per_iteration", "content_type", "hook_mode", "target_beat_audio_s", "max_beat_audio_s", "style_sample", "style_preset", "style_strength", "target_sentence_chars", "max_sentence_chars", "non_story_tail_s", "chatgpt_profile_dir", "chatgpt_session_file", "chat_session_policy", "chat_session_meta", "chat_title", "reply_timeout_s", "llm_backend", "log_level"):
            add_option(command, key, section.get(key))
        command.append("--style-qa" if section.get("style_qa", True) else "--no-style-qa")
        command.append("--opening-coherence-qa" if section.get("opening_coherence_qa", section.get("content_type") == "movie") else "--no-opening-coherence-qa")
        command.append("--micro-beats" if section.get("micro_beats", False) else "--no-micro-beats")
        command.append("--drop-non-story-beats" if section.get("drop_non_story_beats", True) else "--no-drop-non-story-beats")
        if section.get("headless"):
            command.append("--headless")
    elif stage == "tts":
        command += ["--review-script", str(paths.review_script), "--output-audio", str(paths.voiceover), "--output-timing", str(paths.beats_timing)]
        for key in ("voice_id", "provider_mode", "genmax_voice_id", "model", "speed", "inter_beat_pause", "concurrency", "cost_per_1k_chars", "log_level"):
            add_option(command, key, section.get(key))
        add_option(command, "tts_text_normalization", section.get("text_normalization"))
        add_option(command, "tts_pronunciation_lexicon", section.get("pronunciation_lexicon"))
        add_option(command, "tts_normalized_script_output", section.get("normalized_script_output"))
        add_option(command, "tts_normalization_report", section.get("normalization_report"))
        add_option(command, "pronunciation_qa_output", section.get("pronunciation_qa_output"))
        add_option(command, "pronunciation_suggest_backend", section.get("pronunciation_suggest_backend"))
        add_option(command, "lexicon_candidates_output", section.get("lexicon_candidates_output"))
        if section.get("pronunciation_qa", True):
            command.append("--pronunciation-qa")
        else:
            command.append("--no-pronunciation-qa")
        command += ["--film-meta", str(paths.film_map_meta)]
        if not section.get("normalize", True):
            command.append("--no-normalize")
    elif stage == "shots":
        command += ["--input", str(film), "--output", str(paths.shots), "--thumb-dir", str(paths.shots_dir)]
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.exists():
            command += ["--video-profile", str(paths.video_profile)]
        for key in ("detector", "min_shot_len", "sample_frames", "frame_sampling", "face_detection", "min_brightness", "skip_intro", "skip_outro", "downscale", "scene_threshold", "scene_scale_width", "scene_min_gap", "max_shot_len", "log_level"):
            add_option(command, key, section.get(key))
    elif stage == "visual_index":
        command += ["--film", str(film), "--shots", str(paths.shots), "--output", str(paths.shot_visual_index), "--asset-dir", str(paths.visual_index_dir)]
        for key in ("embedding_mode", "embedding_model", "device", "batch_size", "keyframes_per_shot", "frame_sampling", "log_level"):
            add_option(command, key, section.get(key))
    elif stage == "match":
        command += ["--review-script", str(paths.review_script), "--beats-timing", str(paths.beats_timing), "--shots", str(paths.shots), "--output", str(paths.edl)]
        review_intent_setting = section.get("review_intent", "auto")
        if review_intent_setting == "auto" and paths.review_intent.is_file():
            command += ["--review-intent", str(paths.review_intent)]
        elif review_intent_setting not in {None, "auto"}:
            command += ["--review-intent", str(review_intent_setting)]
        story_map_setting = section.get("story_map", "auto")
        if story_map_setting == "auto" and paths.story_map.is_file():
            command += ["--story-map", str(paths.story_map)]
        elif story_map_setting not in {None, "auto"}:
            command += ["--story-map", str(story_map_setting)]
        film_map_setting = section.get("film_map", "auto")
        if film_map_setting == "auto":
            command += ["--film-map", str(paths.film_map)]
        elif film_map_setting:
            command += ["--film-map", str(film_map_setting)]
        output_qa = section.get("output_qa")
        command += ["--output-qa", str(output_qa or paths.edl_qa)]
        command += ["--output-sync-qa", str(paths.edl_sync_qa)]
        command += ["--output-visual-qa", str(section.get("output_visual_qa") or paths.edl_visual_qa)]
        visual_index_setting = section.get("visual_index", "auto")
        if visual_index_setting == "auto" and (paths.shot_visual_index.is_file() or config.get("visual_index", {}).get("enabled", False)):
            command += ["--visual-index", str(paths.shot_visual_index)]
        elif visual_index_setting not in {None, "auto"}:
            command += ["--visual-index", str(visual_index_setting)]
        output_review_html = section.get("output_review_html")
        review_asset_dir = section.get("review_asset_dir")
        command += ["--output-review-html", str(output_review_html or paths.edl_review_html)]
        command += ["--review-asset-dir", str(review_asset_dir or paths.edl_review_dir)]
        add_option(command, "review_thumbs_per_beat", section.get("review_thumbs_per_beat"))
        for key in ("min_clip", "max_clip", "min_visual_clip", "widen_margin", "max_widen", "seed", "max_repeat_per_beat", "max_repeat_ratio_per_beat", "min_repeat_alternative_score_ratio", "adjacent_shot_repeat_penalty", "opening_guard_s", "opening_max_repeat_ratio", "opening_max_repeat_per_shot", "opening_min_unique_shots", "w_motion", "w_face", "w_bright", "w_reuse", "w_semantic", "w_visual", "min_semantic_score", "match_strategy", "chronology_weight", "max_source_drift_s", "semantic_mode", "semantic_model", "semantic_device", "semantic_batch_size", "semantic_cache_dir", "visual_mode", "visual_cache_dir", "visual_device", "visual_batch_size", "log_level"):
            add_option(command, key, section.get(key))
        command.append("--allow-dark-fallback" if section.get("allow_dark_fallback", True) else "--no-allow-dark-fallback")
        command.append("--allow-repeat" if section.get("allow_repeat", True) else "--no-allow-repeat")
        command.append("--allow-speedfit" if section.get("allow_speedfit", False) else "--no-allow-speedfit")
        command.append("--exclude-non-story" if section.get("exclude_non_story", True) else "--no-exclude-non-story")
        command.append("--opening-story-visual-start" if section.get("opening_story_visual_start", True) else "--no-opening-story-visual-start")
        command.append("--opening-allow-short-fill" if section.get("opening_allow_short_fill", True) else "--no-opening-allow-short-fill")
        command.append("--opening-ordered-fill" if section.get("opening_ordered_fill", True) else "--no-opening-ordered-fill")
        command.append("--ordered-fill-by-audio-progress" if section.get("ordered_fill_by_audio_progress", True) else "--no-ordered-fill-by-audio-progress")
        if not section.get("review_html", True):
            command.append("--no-review-html")
    elif stage == "render":
        command += ["--edl", str(paths.edl), "--voiceover", str(paths.voiceover), "--film", str(film), "--output", str(paths.recap)]
        for key in ("width", "height", "fps", "fit", "crf", "preset", "concurrency", "audio_delay_s", "log_level"):
            add_option(command, key, section.get(key))
    else:
        raise OrchestratorError(f"unknown stage: {stage}")
    command += ["--work-dir", str(stage_work(paths, stage))]
    if force:
        command.append("--force")
    return command


def preflight(*, film: Path, selected: set[str], forced: set[str], paths: RunPaths, config: dict[str, Any], dry_run: bool = False, cost_policy: CostPolicy | None = None) -> None:
    if not film.is_file():
        raise OrchestratorError(f"input film does not exist: {film}")
    if selected & {"ingest", "tts", "shots", "visual_index", "render"} and not dry_run:
        require_ffmpeg()
    will_run = {stage for stage in selected if stage in forced or not outputs_valid(paths, stage, film=film, config=config)}
    if cost_policy is not None:
        blocked = disallowed_openai_stages(cost_policy, will_run)
        if blocked:
            raise OrchestratorError("api_budget_guard=block forbids OpenAI usage: " + "; ".join(blocked))
    ingest_policy = cost_policy.stages.get("ingest", {}) if cost_policy is not None else {}
    ingest_needs_openai = bool(ingest_policy.get("openai_uses")) if ingest_policy else True
    if "ingest" in will_run and ingest_needs_openai and not os.getenv("OPENAI_API_KEY") and not dry_run:
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
    if not force and outputs_valid(paths, stage, film=film, config=config):
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

