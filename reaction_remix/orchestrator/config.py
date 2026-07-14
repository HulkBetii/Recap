from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = "reaction-remix.v1"
PIPELINE_NAME = "reaction_remix"
CANONICAL_PROFILE = "D:/VibeCoding/auto_YT/data/chrome_user_data/PROFILE_GPT_1"


class ReactionConfigError(ValueError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "pipeline": PIPELINE_NAME,
    "orchestrator": {
        "python": None,
        "log_level": "INFO",
        "runtime_preflight": True,
        "max_repair_rounds": 2,
    },
    "probe": {"soft_subtitle_policy": "fail", "burned_subtitle_policy": "preserve"},
    "analyze": {
        "whisper_model": "large-v3",
        "device": "cuda",
        "compute_type": "float16",
        "source_language": "auto",
        "max_region_s": 30.0,
        "multilingual_window_s": 6.0,
        "region_overlap_s": 2.0,
        "speech_padding_s": 0.12,
        "word_timestamps": True,
        "max_attempts": 2,
        "speaker_cluster_threshold": 0.28,
    },
    "shots": {
        "enabled": True,
        "detector": "adaptive",
        "frame_sampling": "batch",
        "face_detection": "off",
        "max_shot_len": 0,
    },
    "stems": {
        "enabled": True,
        "provider": "demucs",
        "model": "htdemucs",
        "device": "cuda",
        "output_stem": "no_vocals",
        "leakage_fallback": "tts",
    },
    "segment": {
        "min_silence_s": 0.25,
        "speech_padding_s": 0.12,
        "scene_cut_tolerance_s": 0.5,
        "min_cut_spacing_s": 0.08,
        "commentary_min_confidence": 0.90,
        "narrator_min_regions": 3,
        "narrator_min_japanese_ratio": 0.90,
        "commentary_boundary_policy": "strict_or_word_edge",
    },
    "plan": {
        "llm_backend": "chatgpt_playwright",
        "chatgpt_profile_dir": CANONICAL_PROFILE,
        "chat_session_policy": "auto",
        "playwright_max_attempts": 2,
        "playwright_recovery_timeout_s": 60,
        "reply_timeout_s": 1200,
        "output_ratio": 0.875,
        "hard_min_output_ratio": 0.80,
        "preferred_min_output_ratio": 0.85,
        "preferred_max_output_ratio": 0.90,
        "hard_max_output_ratio": 1.00,
        "min_unique_reaction_speech_ratio": 0.90,
        "allow_block_reuse": False,
        "preserve_ambiguous_blocks": True,
        "manual_drop_block_ids": [],
    },
    "write": {
        "llm_backend": "chatgpt_playwright",
        "language": "ja",
        "style_id": "reaction-internet-ja-v1",
        "chars_per_second": 6.5,
        "max_qa_iterations": 2,
        "require_evidence_block_ids": True,
    },
    "tts": {
        "provider": "ai33",
        "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
        "model": "eleven_multilingual_v2",
        "speed": 1.0,
        "concurrency": 1,
        "fallback_provider": None,
        "text_normalization": "ja_basic",
        "trim_handle_ms": 80,
        "target_lufs": -14.0,
        "max_true_peak_db": -2.0,
        "asr_model": "large-v3",
        "asr_language": "ja",
        "min_asr_similarity": 0.90,
        "fit_tolerance_ms": 100,
        "max_fit_iterations": 2,
    },
    "compose": {
        "commentary_visual_priority": ["commentary"],
        "allow_commentary_visual_reuse": False,
        "commentary_audio_mode": "tts_bed",
        "commentary_tts_gain_db": 1.0,
        "commentary_bed_gain_db": -14.0,
        "commentary_bed_fade_ms": 180,
        "commentary_boundary_fade_ms": 50,
        "commentary_limiter_db": -1.5,
        "bed_leakage_fallback": "tts",
        "max_silent_transition_s": 0.5,
    },
    "render": {
        "require_cfr_source": True,
        "video_codec": "libx264",
        "pixel_format": "yuv420p",
        "crf": 18,
        "preset": "medium",
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        "movflags": "+faststart",
    },
    "qa": {
        "min_output_ratio": 0.80,
        "preferred_min_output_ratio": 0.85,
        "preferred_max_output_ratio": 0.90,
        "min_unique_reaction_speech_ratio": 0.90,
        "min_reaction_audio_correlation": 0.98,
        "max_reaction_av_drift_frames": 1,
        "preferred_av_drift_ms": 20,
        "max_reaction_gain_delta_db": 0.3,
        "min_frame_similarity": 0.995,
        "min_tts_asr_similarity": 0.90,
        "max_narrator_leakage_count": 0,
        "max_commentary_true_peak_db": -1.5,
        "commentary_peak_encode_tolerance_db": 0.3,
        "max_full_output_peak_increase_db": 0.3,
        "unexpected_silence_s": 0.25,
    },
}


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _validate_known_keys(data: dict[str, Any], defaults: dict[str, Any], prefix: str = "") -> None:
    unknown = set(data) - set(defaults)
    if unknown:
        names = ", ".join(f"{prefix}{name}" for name in sorted(unknown))
        raise ReactionConfigError(f"unknown config key(s): {names}")
    for key, value in data.items():
        expected = defaults.get(key)
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ReactionConfigError(f"config section {prefix}{key} must be an object")
            _validate_known_keys(value, expected, f"{prefix}{key}.")


def _validate_locked_policy(config: dict[str, Any]) -> None:
    if config["schema_version"] != SCHEMA_VERSION:
        raise ReactionConfigError(f"schema_version must be {SCHEMA_VERSION}")
    if config["pipeline"] != PIPELINE_NAME:
        raise ReactionConfigError(f"pipeline must be {PIPELINE_NAME}")
    boundary_policy = str(config["segment"]["commentary_boundary_policy"]).replace("_", "-")
    if boundary_policy not in {"strict", "strict-or-word-edge"}:
        raise ReactionConfigError(
            "segment.commentary_boundary_policy must be strict or strict_or_word_edge"
        )
    if config["plan"]["llm_backend"] != "chatgpt_playwright" or config["write"]["llm_backend"] != "chatgpt_playwright":
        raise ReactionConfigError("reaction planning and writing require chatgpt_playwright")
    profile = Path(config["plan"]["chatgpt_profile_dir"]).as_posix().rstrip("/").lower()
    if profile != CANONICAL_PROFILE.lower():
        raise ReactionConfigError(f"chatgpt_profile_dir must be {CANONICAL_PROFILE}")
    tts = config["tts"]
    locked_tts = {
        "provider": "ai33",
        "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
        "model": "eleven_multilingual_v2",
        "speed": 1.0,
        "concurrency": 1,
        "fallback_provider": None,
        "text_normalization": "ja_basic",
    }
    for key, expected in locked_tts.items():
        if tts[key] != expected:
            raise ReactionConfigError(f"tts.{key} is locked to {expected!r}")
    plan = config["plan"]
    ratios = (
        plan["hard_min_output_ratio"],
        plan["preferred_min_output_ratio"],
        plan["output_ratio"],
        plan["preferred_max_output_ratio"],
        plan["hard_max_output_ratio"],
    )
    if tuple(sorted(ratios)) != ratios or ratios[0] < 0.80 or ratios[-1] > 1.0:
        raise ReactionConfigError("plan output ratios must be ordered inside the hard 0.80-1.00 range")
    if plan["min_unique_reaction_speech_ratio"] < 0.90:
        raise ReactionConfigError("unique reaction speech retention must be at least 0.90")
    manual_drop_ids = plan["manual_drop_block_ids"]
    if not isinstance(manual_drop_ids, list) or not all(isinstance(value, str) for value in manual_drop_ids):
        raise ReactionConfigError("plan.manual_drop_block_ids must be a list of block IDs")
    if len(set(manual_drop_ids)) != len(manual_drop_ids):
        raise ReactionConfigError("plan.manual_drop_block_ids must not contain duplicates")
    if config["render"]["crf"] != 18:
        raise ReactionConfigError("render.crf is locked to 18 for reaction-remix.v1")
    locked_values = {
        "plan.allow_block_reuse": (config["plan"]["allow_block_reuse"], False),
        "plan.preserve_ambiguous_blocks": (config["plan"]["preserve_ambiguous_blocks"], True),
        "write.chars_per_second": (config["write"]["chars_per_second"], 6.5),
        "write.require_evidence_block_ids": (config["write"]["require_evidence_block_ids"], True),
        "tts.asr_language": (config["tts"]["asr_language"], "ja"),
        "stems.output_stem": (config["stems"]["output_stem"], "no_vocals"),
        "stems.leakage_fallback": (config["stems"]["leakage_fallback"], "tts"),
        "compose.commentary_visual_priority": (
            config["compose"]["commentary_visual_priority"],
            ["commentary"],
        ),
        "compose.allow_commentary_visual_reuse": (
            config["compose"]["allow_commentary_visual_reuse"],
            False,
        ),
        "compose.commentary_audio_mode": (config["compose"]["commentary_audio_mode"], "tts_bed"),
        "compose.commentary_limiter_db": (config["compose"]["commentary_limiter_db"], -1.5),
        "compose.bed_leakage_fallback": (config["compose"]["bed_leakage_fallback"], "tts"),
        "compose.max_silent_transition_s": (config["compose"]["max_silent_transition_s"], 0.5),
        "render.require_cfr_source": (config["render"]["require_cfr_source"], True),
        "render.video_codec": (config["render"]["video_codec"], "libx264"),
        "render.pixel_format": (config["render"]["pixel_format"], "yuv420p"),
        "render.audio_codec": (config["render"]["audio_codec"], "aac"),
        "render.movflags": (config["render"]["movflags"], "+faststart"),
        "qa.max_reaction_av_drift_frames": (config["qa"]["max_reaction_av_drift_frames"], 1),
        "qa.preferred_av_drift_ms": (config["qa"]["preferred_av_drift_ms"], 20),
        "qa.max_reaction_gain_delta_db": (config["qa"]["max_reaction_gain_delta_db"], 0.3),
        "qa.max_narrator_leakage_count": (config["qa"]["max_narrator_leakage_count"], 0),
        "qa.max_commentary_true_peak_db": (config["qa"]["max_commentary_true_peak_db"], -1.5),
        "qa.commentary_peak_encode_tolerance_db": (
            config["qa"]["commentary_peak_encode_tolerance_db"],
            0.3,
        ),
        "qa.max_full_output_peak_increase_db": (config["qa"]["max_full_output_peak_increase_db"], 0.3),
        "qa.unexpected_silence_s": (config["qa"]["unexpected_silence_s"], 0.25),
    }
    for name, (actual, expected) in locked_values.items():
        if actual != expected:
            raise ReactionConfigError(f"{name} is locked to {expected!r} in reaction-remix.v1")


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        config = deepcopy(DEFAULT_CONFIG)
    else:
        if not path.is_file():
            raise ReactionConfigError(f"config file does not exist: {path}")
        suffix = path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise ReactionConfigError("config must be .yaml, .yml, or .json")
        if not isinstance(data, dict):
            raise ReactionConfigError("config root must be an object")
        _validate_known_keys(data, DEFAULT_CONFIG)
        config = _deep_merge(DEFAULT_CONFIG, data)
    _validate_locked_policy(config)
    return config
