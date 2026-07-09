from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without optional dep
    yaml = None  # type: ignore[assignment]

STAGE_NAMES = ("preflight", "ingest", "storymap", "review", "tts", "tts_align", "shots", "match", "render")
TOP_LEVEL_KEYS = set(STAGE_NAMES) | {"orchestrator"}

DEFAULT_CONFIG: dict[str, Any] = {
    "orchestrator": {"python": None, "log_level": "INFO", "quality_mode": "balanced", "text_llm_backend": "chatgpt_playwright", "api_budget_guard": "warn", "auto_fallback": False, "fallback_on_timecode_warn": True, "fallback_ingest_asr_provider": "openai-gpt4o-hybrid", "fallback_max_vision_frames": 0},
    "preflight": {
        "enabled": True,
        "max_intro_s": 240.0,
        "sample_every_s": 5.0,
        "classifier": "heuristic",
        "confidence_threshold": 0.75,
        "uncertain_threshold": 0.55,
        "log_level": "INFO",
    },
    "ingest": {
        "whisper_model": "large-v3",
        "gap_threshold": 4.0,
        "max_vision_frames": 200,
        "max_visual_gap_s": 20.0,
        "translate_model": "gpt-4.1-mini",
        "source_language": "ko",
        "translate_mode": "ko-en",
        "vision_model": "gpt-4.1-mini",
        "device": "cpu",
        "asr_provider": "faster-whisper",
        "aligner": "none",
        "transcript_input": None,
        "timecode_quality": "strict",
        "max_segment_s": 30.0,
        "merge_gap_s": 0.0,
        "vad_filter": True,
        "openai_transcribe_model": "gpt-4o-mini-transcribe",
        "openai_chunk_s": 20.0,
        "alignment_device": "cuda",
        "transcript_correction": "off",
        "glossary": None,
        "correction_model": "gpt-4.1-mini",
        "drop_non_korean_intro_s": 30.0,
        "drop_visual_before_s": 0.0,
        "asr_policy": "preset",
        "log_level": "INFO",
    },
    "storymap": {
        "enabled": True,
        "content_type": "movie",
        "target_story_sections": 7,
        "log_level": "INFO",
    },
    "review": {
        "target_ratio": "auto",
        "content_type": "movie",
        "hook_mode": "setup",
        "opening_coherence_qa": True,
        "max_qa_rewrites_per_iteration": 6,
        "micro_beats": False,
        "target_beat_audio_s": 12.0,
        "max_beat_audio_s": 18.0,
        "story_map": "auto",
        "review_intent_output": None,
        "tts_cps": 15,
        "min_coverage": 0.85,
        "max_qa_iterations": 3,
        "chatgpt_profile_dir": "data/chrome_user_data/PROFILE_GPT_1",
        "chatgpt_session_file": None,
        "chat_session_policy": "auto",
        "chat_session_meta": None,
        "chat_title": None,
        "reply_timeout_s": 600,
        "llm_backend": "chatgpt_playwright",
        "style_sample": "examples/style/viral_recap_vi.cleaned.txt",
        "style_preset": "viral-recap-vi",
        "style_strength": "strong",
        "style_qa": True,
        "target_sentence_chars": 160,
        "max_sentence_chars": 220,
        "drop_non_story_beats": True,
        "non_story_tail_s": 300.0,
        "headless": False,
        "log_level": "INFO",
    },
    "tts": {
        "voice_id": None,
        "provider_mode": "auto",
        "genmax_voice_id": None,
        "model": "eleven_multilingual_v2",
        "speed": 1.0,
        "inter_beat_pause": 0.15,
        "concurrency": 3,
        "normalize": True,
        "cost_per_1k_chars": 0.0,
        "text_normalization": "vi",
        "pronunciation_lexicon": None,
        "normalized_script_output": None,
        "normalization_report": None,
        "pronunciation_qa": True,
        "pronunciation_qa_output": None,
        "pronunciation_suggest_backend": None,
        "lexicon_candidates_output": None,
        "log_level": "INFO",
    },
    "tts_align": {
        "mode": "auto",
        "max_source_span_s": 120,
        "max_narration_chars": 520,
        "min_sentences": 2,
        "target_sub_beat_audio_s": 8,
        "max_sub_beat_audio_s": 12,
        "split_hooks": True,
        "aligner": "whisperx",
        "alignment_device": "auto",
        "source_language": "vi",
        "log_level": "INFO",
    },
    "shots": {
        "detector": "adaptive",
        "min_shot_len": 0.4,
        "sample_frames": 5,
        "face_detection": "on",
        "min_brightness": 0.06,
        "skip_intro": 0.0,
        "skip_outro": 0.0,
        "downscale": "auto",
        "log_level": "INFO",
    },
    "match": {
        "min_clip": 3.0,
        "max_clip": 5.0,
        "widen_margin": 15.0,
        "max_widen": 3,
        "allow_repeat": True,
        "allow_speedfit": False,
        "exclude_non_story": True,
        "max_repeat_per_beat": 2,
        "max_repeat_ratio_per_beat": 0.35,
        "min_repeat_alternative_score_ratio": 0.75,
        "adjacent_shot_repeat_penalty": 0.50,
        "opening_guard_s": 120.0,
        "opening_max_repeat_ratio": 0.20,
        "opening_max_repeat_per_shot": 1,
        "opening_min_unique_shots": 4,
        "opening_story_visual_start": True,
        "opening_allow_short_fill": True,
        "seed": 1234,
        "w_motion": 0.60,
        "w_face": 0.18,
        "w_bright": 0.12,
        "w_reuse": 0.35,
        "w_semantic": 0.15,
        "min_semantic_score": 0.22,
        "match_strategy": "chronological",
        "chronology_weight": 0.70,
        "max_source_drift_s": 12.0,
        "semantic_mode": "bge-m3",
        "semantic_model": "BAAI/bge-m3",
        "semantic_device": "auto",
        "semantic_batch_size": 16,
        "semantic_cache_dir": None,
        "film_map": "auto",
        "output_qa": None,
        "output_review_html": None,
        "review_asset_dir": None,
        "review_thumbs_per_beat": 8,
        "review_intent": "auto",
        "review_micro": "auto",
        "story_map": "auto",
        "opening_ordered_fill": True,
        "ordered_fill_by_audio_progress": True,
        "review_html": True,
        "log_level": "INFO",
    },
    "render": {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "fit": "cover",
        "crf": 20,
        "preset": "medium",
        "concurrency": 4,
        "audio_delay_s": 0.0,
        "bgm": {
            "enabled": False,
            "path": None,
            "gain_db": -20.0,
            "fade_in_s": 1.5,
            "fade_out_s": 2.5,
            "ducking": "none",
        },
        "captions": {
            "enabled": False,
            "font_name": "Arial",
            "font_size": 54,
            "margin_v": 64,
            "outline": 3,
            "max_chars_per_line": 42,
            "max_lines": 2,
        },
        "log_level": "INFO",
    },
}

class ConfigError(ValueError):
    pass


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if key not in merged:
            raise ConfigError(f"unknown config section: {key}")
        if isinstance(value, dict) and isinstance(merged[key], dict):
            unknown = set(value) - set(merged[key])
            if unknown:
                raise ConfigError(f"unknown config key(s) in {key}: {', '.join(sorted(unknown))}")
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return deepcopy(DEFAULT_CONFIG)
    if not path.is_file():
        raise ConfigError(f"config file does not exist: {path}")
    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        data = json.loads(raw_text)
    elif suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigError("PyYAML is required to read YAML config files")
        data = yaml.safe_load(raw_text) or {}
    else:
        raise ConfigError("config must be .yaml, .yml or .json")
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    merged = deep_merge(DEFAULT_CONFIG, data)
    apply_content_type_match_defaults(merged, data)
    return merged

def apply_content_type_match_defaults(config: dict[str, Any], raw_overrides: dict[str, Any]) -> None:
    review = config.get("review", {})
    match = config.get("match", {})
    raw_match = raw_overrides.get("match", {}) if isinstance(raw_overrides.get("match", {}), dict) else {}
    if review.get("content_type") != "episode":
        return
    if "match_strategy" not in raw_match:
        match["match_strategy"] = "hybrid"
    if "w_semantic" not in raw_match:
        match["w_semantic"] = 0.45


def flag_name(key: str) -> str:
    return "--" + key.replace("_", "-")


def add_option(args: list[str], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            args.append(flag_name(key))
        return
    args.extend([flag_name(key), str(value)])

