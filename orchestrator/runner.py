from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable

from common.integrity import file_hash, media_identity_hash
from common.media import require_ffmpeg
from common.schema import (
    BeatTiming,
    EdlMeta,
    EdlPlacement,
    EpisodeMemory,
    EpisodeMeta,
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
from episode_planner.integrity import EPISODE_PLANNER_CACHE_VERSION, episode_planner_config_hash, episode_planner_input_hashes
from tts.providers import TtsProviderError, resolve_provider_order
from visual_index.integrity import metadata_is_current, validate_visual_index_artifacts, visual_index_config_hash
from match.version import MATCH_ALGORITHM_VERSION
from ingest.integrity import INGEST_CACHE_VERSION, ingest_config_hash
from preflight.integrity import PREFLIGHT_CACHE_VERSION, preflight_identity
from review.integrity import REVIEW_CACHE_VERSION, build_review_identity
from review.style import DEFAULT_STYLE_SAMPLE
from storymap.cache import stable_hash as storymap_stable_hash

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
    "episode_planner": StageSpec("episode_planner", ("episode_meta", "episode_memory"), "episode_meta"),
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


def episode_planner_enabled(config: dict[str, Any]) -> bool:
    section = config.get("orchestrator", {})
    return str(section.get("recap_mode", "off")) != "off"


def read_episode_meta(paths: RunPaths) -> EpisodeMeta | None:
    if not paths.episode_meta.is_file():
        return None
    return EpisodeMeta.model_validate(load_json(paths.episode_meta))


def effective_review_section(paths: RunPaths, config: dict[str, Any]) -> dict[str, Any]:
    section = deepcopy(config.get("review", {}))
    planner_meta = read_episode_meta(paths)
    if planner_meta is None:
        return section
    orchestrator = config.get("orchestrator", {})
    if not section.get("context_file") and paths.episode_memory.is_file():
        section["context_file"] = str(paths.episode_memory)
    if planner_meta.recap_mode == "quick":
        quick_ratio = planner_meta.quick_target_ratio or float(orchestrator.get("quick_target_ratio", 0.12))
        if section.get("target_ratio", "auto") == "auto":
            section["target_ratio"] = quick_ratio
        quick_min_coverage = float(orchestrator.get("quick_min_coverage", 0.45))
        if float(section.get("min_coverage", 0.0)) > quick_min_coverage:
            section["min_coverage"] = quick_min_coverage
        if int(section.get("max_qa_iterations", 3)) > 1:
            section["max_qa_iterations"] = 1
        if int(section.get("max_qa_rewrites_per_iteration", 6)) > 2:
            section["max_qa_rewrites_per_iteration"] = 2
    return section


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
        elif stage == "episode_planner":
            meta = EpisodeMeta.model_validate(load_json(paths.episode_meta))
            memory = EpisodeMemory.model_validate(load_json(paths.episode_memory))
            if meta.series_id != memory.current.series_id or meta.episode_key != memory.current.episode_key:
                raise ValueError("episode_meta and episode_memory current identity do not match")
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
        if stage == "preflight" and film is not None and config is not None:
            section = config.get("preflight", {})
            expected_input, expected_config = preflight_identity(
                film,
                classifier=str(section.get("classifier", "heuristic")),
                max_intro_s=float(section.get("max_intro_s", 240.0)),
                sample_every_s=float(section.get("sample_every_s", 5.0)),
                confidence_threshold=float(section.get("confidence_threshold", 0.75)),
                uncertain_threshold=float(section.get("uncertain_threshold", 0.55)),
                manual_ranges_hash=file_hash(Path(str(section["manual_ranges"]))) if section.get("manual_ranges") else None,
                anime_context_hash=file_hash(Path(str(section["anime_context"]))) if section.get("anime_context") else None,
            )
            profile = VideoProfile.model_validate(load_json(paths.video_profile))
            if profile.cache_version != PREFLIGHT_CACHE_VERSION or profile.input_hash != expected_input or profile.config_hash != expected_config:
                return False
        if stage == "ingest" and film is not None and config is not None:
            meta = FilmMapMeta.model_validate(load_json(paths.film_map_meta))
            profile_path = paths.video_profile if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file() else None
            if (
                meta.cache_version != INGEST_CACHE_VERSION
                or meta.input_hash != media_identity_hash(film)
                or meta.config_hash != ingest_config_hash(config.get("ingest", {}))
                or meta.video_profile_hash != file_hash(profile_path)
            ):
                return False
        if stage == "storymap" and config is not None:
            section = config.get("storymap", {})
            profile_path = paths.video_profile if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file() else None
            profile_payload = VideoProfile.model_validate(load_json(profile_path)).model_dump(mode="json") if profile_path else None
            expected_config = storymap_stable_hash(
                {
                    "film_map": storymap_stable_hash(load_json(paths.film_map)),
                    "video_profile": storymap_stable_hash(profile_payload),
                    "content_type": section.get("content_type", "movie"),
                    "target_story_sections": section.get("target_story_sections", 7),
                }
            )
            meta = StoryMapMeta.model_validate(load_json(paths.story_map_meta))
            if (
                meta.cache_version != "storymap-v1"
                or meta.film_map_hash != file_hash(paths.film_map)
                or meta.video_profile_hash != file_hash(profile_path)
                or meta.config_hash != expected_config
            ):
                return False
        if stage == "episode_planner" and film is not None and config is not None:
            section = config.get("orchestrator", {})
            profile_path = paths.video_profile if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file() else None
            anime_context_setting = config.get("preflight", {}).get("anime_context") or config.get("review", {}).get("context_file")
            anime_context_path = Path(str(anime_context_setting)).expanduser().resolve() if anime_context_setting else None
            series_manifest_setting = section.get("series_manifest")
            series_manifest_path = Path(str(series_manifest_setting)).expanduser().resolve() if series_manifest_setting else None
            expected_hashes = episode_planner_input_hashes(
                film=film,
                film_map_path=paths.film_map,
                story_map_path=paths.story_map if paths.story_map.is_file() else None,
                video_profile_path=profile_path,
                anime_context_path=anime_context_path,
                series_manifest_path=series_manifest_path,
            )
            expected_section = deepcopy(section)
            if expected_section.get("recap_mode") == "off":
                expected_section["recap_mode"] = "auto"
            expected_config = episode_planner_config_hash(expected_section)
            meta = EpisodeMeta.model_validate(load_json(paths.episode_meta))
            if (
                meta.cache_version != EPISODE_PLANNER_CACHE_VERSION
                or meta.source_hash != expected_hashes["source_hash"]
                or meta.film_map_hash != expected_hashes["film_map_hash"]
                or meta.story_map_hash != expected_hashes["story_map_hash"]
                or meta.video_profile_hash != expected_hashes["video_profile_hash"]
                or meta.anime_context_hash != expected_hashes["anime_context_hash"]
                or meta.series_manifest_hash != expected_hashes["series_manifest_hash"]
                or meta.config_hash != expected_config
            ):
                return False
        if stage == "review" and config is not None:
            section = effective_review_section(paths, config)
            story_setting = section.get("story_map", "auto")
            story_path = paths.story_map if story_setting == "auto" and paths.story_map.is_file() else (Path(str(story_setting)) if story_setting not in {None, "auto"} else None)
            profile_path = paths.video_profile if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file() else None
            style_setting = section.get("style_sample")
            style_path = Path(str(style_setting)).expanduser().resolve() if style_setting else DEFAULT_STYLE_SAMPLE
            context_setting = section.get("context_file")
            context_path = Path(str(context_setting)).expanduser().resolve() if context_setting else None
            identity = build_review_identity(
                film_map_path=paths.film_map,
                settings=section,
                style_sample_path=style_path,
                story_map_path=story_path,
                video_profile_path=profile_path,
                context_file_path=context_path,
            )
            meta = ReviewMeta.model_validate(load_json(paths.review_meta))
            if (
                meta.cache_version != REVIEW_CACHE_VERSION
                or meta.film_map_hash != identity.film_map_hash
                or meta.film_map_meta_hash != identity.film_map_meta_hash
                or meta.story_map_hash != identity.story_map_hash
                or meta.video_profile_hash != identity.video_profile_hash
                or meta.context_file_hash != identity.context_file_hash
                or meta.config_hash != identity.config_hash
            ):
                return False
        if stage == "shots" and config is not None:
            section = config.get("shots", {})
            meta = ShotsMeta.model_validate(load_json(paths.shots_meta))
            expected_guard = bool(section.get("end_credit_guard", False))
            if bool(meta.feature_config.get("end_credit_guard", False)) != expected_guard:
                return False
            if expected_guard:
                if float(meta.feature_config.get("end_credit_tail_s", 0.0)) != float(section.get("end_credit_tail_s", 600.0)):
                    return False
                if float(meta.feature_config.get("end_credit_threshold", 0.0)) != float(section.get("end_credit_threshold", 0.60)):
                    return False
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
    except (OrchestratorError, OSError, ValueError):
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
            for key in ("max_intro_s", "sample_every_s", "classifier", "confidence_threshold", "uncertain_threshold", "manual_ranges", "anime_context", "log_level"):
                add_option(command, key, section.get(key))
        else:
            command += ["--classifier", "heuristic"]
    elif stage == "ingest":
        command += ["--input", str(film), "--output", str(paths.film_map)]
        for key in ("whisper_model", "gap_threshold", "max_vision_frames", "max_visual_gap_s", "translate_model", "source_language", "translate_mode", "vision_model", "device", "asr_provider", "aligner", "transcript_input", "timecode_quality", "max_segment_s", "merge_gap_s", "openai_transcribe_model", "openai_chunk_s", "alignment_device", "transcript_correction", "glossary", "correction_model", "drop_non_korean_intro_s", "drop_visual_before_s", "log_level"):
            add_option(command, key, section.get(key))
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file():
            command += ["--video-profile", str(paths.video_profile)]
        if not section.get("vad_filter", True):
            command.append("--no-vad-filter")
    elif stage == "storymap":
        command += ["--film-map", str(paths.film_map), "--output", str(paths.story_map), "--output-qa", str(paths.story_map_qa)]
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file():
            command += ["--video-profile", str(paths.video_profile)]
        for key in ("content_type", "target_story_sections", "log_level"):
            add_option(command, key, section.get(key))
    elif stage == "episode_planner":
        section = config.get("orchestrator", {})
        command += [
            "--source-path",
            str(film),
            "--film-map",
            str(paths.film_map),
            "--output-meta",
            str(paths.episode_meta),
            "--output-memory",
            str(paths.episode_memory),
        ]
        if config.get("preflight", {}).get("enabled", True) and paths.video_profile.is_file():
            command += ["--video-profile", str(paths.video_profile)]
        if paths.story_map.is_file():
            command += ["--story-map", str(paths.story_map)]
        anime_context_setting = config.get("preflight", {}).get("anime_context") or config.get("review", {}).get("context_file")
        if anime_context_setting:
            command += ["--anime-context", str(anime_context_setting)]
        for key in (
            "series_manifest",
            "series_memory_dir",
            "episode_key",
            "episode_number",
            "recap_full_threshold",
            "recap_quick_threshold",
            "recap_merge_threshold",
            "quick_target_ratio",
            "quick_min_coverage",
            "log_level",
        ):
            add_option(command, key, section.get(key))
        if section.get("recap_mode") not in {None, "off"}:
            add_option(command, "recap_mode", section.get("recap_mode"))
    elif stage == "review":
        section = effective_review_section(paths, config)
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
        for key in ("target_ratio", "tts_cps", "min_coverage", "max_qa_iterations", "max_qa_rewrites_per_iteration", "content_type", "hook_mode", "target_beat_audio_s", "max_beat_audio_s", "style_sample", "style_preset", "style_strength", "target_sentence_chars", "max_sentence_chars", "non_story_tail_s", "context_file", "chatgpt_profile_dir", "chatgpt_session_file", "chat_session_policy", "chat_session_meta", "chat_title", "reply_timeout_s", "llm_backend", "playwright_max_attempts", "playwright_recovery_timeout_s", "openai_fallback_model", "log_level"):
            add_option(command, key, section.get(key))
        if config.get("orchestrator", {}).get("api_budget_guard") == "block":
            command.append("--block-openai-fallback")
        command.append("--style-qa" if section.get("style_qa", True) else "--no-style-qa")
        command.append("--opening-coherence-qa" if section.get("opening_coherence_qa", section.get("content_type") in {"movie", "anime_movie"}) else "--no-opening-coherence-qa")
        command.append("--micro-beats" if section.get("micro_beats", False) else "--no-micro-beats")
        command.append("--drop-non-story-beats" if section.get("drop_non_story_beats", True) else "--no-drop-non-story-beats")
        if section.get("headless"):
            command.append("--headless")
    elif stage == "tts":
        command += ["--review-script", str(paths.review_script), "--output-audio", str(paths.voiceover), "--output-timing", str(paths.beats_timing)]
        for key in ("voice_id", "provider_mode", "genmax_voice_id", "model", "openai_model", "openai_voice", "speed", "inter_beat_pause", "concurrency", "cost_per_1k_chars", "log_level"):
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
        for key in ("detector", "min_shot_len", "sample_frames", "frame_sampling", "face_detection", "min_brightness", "end_credit_tail_s", "end_credit_threshold", "skip_intro", "skip_outro", "downscale", "scene_threshold", "scene_scale_width", "scene_min_gap", "max_shot_len", "log_level"):
            add_option(command, key, section.get(key))
        command.append("--end-credit-guard" if section.get("end_credit_guard", False) else "--no-end-credit-guard")
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
        for key in ("min_clip", "max_clip", "min_visual_clip", "widen_margin", "max_widen", "seed", "max_repeat_per_beat", "max_repeat_ratio_per_beat", "min_repeat_alternative_score_ratio", "adjacent_shot_repeat_penalty", "opening_guard_s", "opening_max_repeat_ratio", "opening_max_repeat_per_shot", "opening_min_unique_shots", "hook_min_brightness", "w_motion", "w_face", "w_bright", "w_reuse", "w_semantic", "w_visual", "min_semantic_score", "match_strategy", "chronology_weight", "max_source_drift_s", "semantic_mode", "semantic_model", "semantic_device", "semantic_batch_size", "semantic_cache_dir", "visual_mode", "visual_cache_dir", "visual_device", "visual_batch_size", "log_level"):
            add_option(command, key, section.get(key))
        command.append("--allow-dark-fallback" if section.get("allow_dark_fallback", True) else "--no-allow-dark-fallback")
        command.append("--content-anchors" if section.get("content_anchors", True) else "--no-content-anchors")
        command.append("--allow-repeat" if section.get("allow_repeat", True) else "--no-allow-repeat")
        command.append("--allow-speedfit" if section.get("allow_speedfit", False) else "--no-allow-speedfit")
        command.append("--exclude-non-story" if section.get("exclude_non_story", True) else "--no-exclude-non-story")
        command.append("--exclude-end-credits" if section.get("exclude_end_credits", False) else "--no-exclude-end-credits")
        command.append("--opening-story-visual-start" if section.get("opening_story_visual_start", True) else "--no-opening-story-visual-start")
        command.append("--opening-allow-short-fill" if section.get("opening_allow_short_fill", True) else "--no-opening-allow-short-fill")
        command.append("--opening-ordered-fill" if section.get("opening_ordered_fill", True) else "--no-opening-ordered-fill")
        command.append("--opening-intra-beat-align" if section.get("opening_intra_beat_align", False) else "--no-opening-intra-beat-align")
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
    if not dry_run and config.get("orchestrator", {}).get("runtime_preflight", False):
        validate_runtime_requirements(will_run, config)
    if cost_policy is not None:
        blocked = disallowed_openai_stages(cost_policy, will_run)
        if blocked:
            raise OrchestratorError("api_budget_guard=block forbids OpenAI usage: " + "; ".join(blocked))
    ingest_policy = cost_policy.stages.get("ingest", {}) if cost_policy is not None else {}
    ingest_needs_openai = bool(ingest_policy.get("openai_uses")) if ingest_policy else True
    if "ingest" in will_run and ingest_needs_openai and not os.getenv("OPENAI_API_KEY") and not dry_run:
        raise OrchestratorError("OPENAI_API_KEY is required to run ingest")
    recap_mode = str(config.get("orchestrator", {}).get("recap_mode", "off"))
    defer_episode_downstream = "episode_planner" in selected and recap_mode in {"auto", "merge", "skip"}
    if "review" in will_run and not dry_run and not defer_episode_downstream:
        profile = Path(str(config["review"].get("chatgpt_profile_dir"))).expanduser()
        if not profile.exists():
            raise OrchestratorError(f"ChatGPT profile dir does not exist for review: {profile}")
    if "tts" in will_run and not dry_run and not defer_episode_downstream:
        tts_config = config["tts"]
        if not tts_config.get("voice_id"):
            raise OrchestratorError("tts.voice_id must be set in config")
        try:
            resolve_provider_order(
                tts_config.get("provider_mode", "auto"),
                voice_id=str(tts_config.get("voice_id", "")),
                genmax_voice_id=tts_config.get("genmax_voice_id"),
            )
        except TtsProviderError as exc:
            raise OrchestratorError(str(exc)) from exc


def runtime_module_available(module: str) -> bool:
    return find_spec(module) is not None


def runtime_cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def validate_runtime_requirements(will_run: set[str], config: dict[str, Any]) -> None:
    modules: set[str] = set()
    cuda_required = False
    ingest = config.get("ingest", {})
    if "ingest" in will_run and ingest.get("aligner") == "whisperx":
        modules.update({"torch", "torchaudio", "whisperx"})
        cuda_required = ingest.get("alignment_device") == "cuda"
    visual_index = config.get("visual_index", {})
    if "visual_index" in will_run and visual_index.get("enabled", False):
        modules.update({"torch", "transformers", "PIL"})
        cuda_required = cuda_required or visual_index.get("device") == "cuda"
    match = config.get("match", {})
    if "match" in will_run and match.get("semantic_mode") == "bge-m3":
        modules.update({"torch", "sentence_transformers"})
        cuda_required = cuda_required or match.get("semantic_device") == "cuda"
    if "match" in will_run and match.get("visual_mode") == "rerank":
        modules.update({"torch", "transformers", "PIL"})
        cuda_required = cuda_required or match.get("visual_device") == "cuda"
    missing = sorted(module for module in modules if not runtime_module_available(module))
    if missing:
        raise OrchestratorError(
            "production runtime dependency missing: " + ", ".join(missing) + '; install with pip install -e ".[movie-visual]"'
        )
    if cuda_required and not runtime_cuda_available():
        raise OrchestratorError("production runtime requires CUDA, but torch.cuda.is_available() is false")


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
    run_anyway: bool = False,
    python_exe: str | None = None,
    executor: Callable[[list[str], Path], None] = run_subprocess,
) -> StageSummary:
    command = build_command(stage, paths, film, config, force, python_exe=python_exe)
    outputs = [str(path) for path in output_paths(paths, stage)]
    if not force and not run_anyway and outputs_valid(paths, stage, film=film, config=config):
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

