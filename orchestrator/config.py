from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without optional dep
    yaml = None  # type: ignore[assignment]

STAGE_NAMES = ("ingest", "review", "tts", "shots", "match", "render")
TOP_LEVEL_KEYS = set(STAGE_NAMES) | {"orchestrator"}

DEFAULT_CONFIG: dict[str, Any] = {
    "orchestrator": {"python": None, "log_level": "INFO"},
    "ingest": {
        "whisper_model": "large-v3",
        "gap_threshold": 4.0,
        "max_vision_frames": 200,
        "translate_model": "gpt-4.1-mini",
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
        "log_level": "INFO",
    },
    "review": {
        "target_ratio": 0.33,
        "tts_cps": 15,
        "min_coverage": 0.85,
        "max_qa_iterations": 3,
        "chatgpt_profile_dir": "data/chrome_user_data/PROFILE_GPT_1",
        "style_sample": None,
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
        "seed": 1234,
        "w_motion": 0.60,
        "w_face": 0.18,
        "w_bright": 0.12,
        "w_reuse": 0.35,
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
    return deep_merge(DEFAULT_CONFIG, data)


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
