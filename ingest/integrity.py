from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from common.integrity import file_hash, stable_hash

INGEST_CACHE_VERSION = "ingest-v1"
INGEST_PREPROCESSING_VERSION = "film-map-v1"

INGEST_CONFIG_FIELDS = (
    "whisper_model",
    "gap_threshold",
    "max_vision_frames",
    "max_visual_gap_s",
    "translate_model",
    "translation_required",
    "translation_min_success_ratio",
    "source_language",
    "translate_mode",
    "vision_provider",
    "vision_model",
    "vision_resize_long_edge",
    "vision_batch_size",
    "device",
    "asr_provider",
    "aligner",
    "timecode_quality",
    "max_segment_s",
    "merge_gap_s",
    "vad_filter",
    "openai_transcribe_model",
    "openai_chunk_s",
    "alignment_device",
    "transcript_correction",
    "correction_model",
    "drop_non_korean_intro_s",
    "drop_visual_before_s",
)


def _value(settings: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def ingest_config_hash(settings: Mapping[str, Any] | object) -> str:
    payload = {name: _value(settings, name) for name in INGEST_CONFIG_FIELDS}
    for name in (
        "gap_threshold",
        "max_visual_gap_s",
        "max_segment_s",
        "merge_gap_s",
        "openai_chunk_s",
        "drop_non_korean_intro_s",
        "drop_visual_before_s",
        "translation_min_success_ratio",
    ):
        if payload.get(name) is not None:
            payload[name] = float(payload[name])
    if payload.get("max_vision_frames") is not None:
        payload["max_vision_frames"] = int(payload["max_vision_frames"])
    if payload.get("vision_resize_long_edge") is not None:
        payload["vision_resize_long_edge"] = int(payload["vision_resize_long_edge"])
    if payload.get("vision_batch_size") is not None:
        payload["vision_batch_size"] = int(payload["vision_batch_size"])
    payload["translation_required"] = bool(payload.get("translation_required"))
    if payload.get("transcript_correction") in {None, False}:
        payload["transcript_correction"] = "off"
    transcript_input = _value(settings, "transcript_input")
    glossary = _value(settings, "glossary")
    payload.update(
        {
            "manual_transcript_hash": file_hash(Path(transcript_input)) if transcript_input else None,
            "glossary_hash": file_hash(Path(glossary)) if glossary else None,
            "preprocessing_version": INGEST_PREPROCESSING_VERSION,
        }
    )
    return stable_hash(payload)


def audio_cache_key(input_hash: str) -> str:
    return stable_hash({"input_hash": input_hash, "audio_format": "mono-16khz-wav-v1"})


def transcript_cache_key(audio_key: str, settings: Mapping[str, Any] | object) -> str:
    transcript_input = _value(settings, "transcript_input")
    return stable_hash(
        {
            "audio_key": audio_key,
            "asr_provider": _value(settings, "asr_provider"),
            "whisper_model": _value(settings, "whisper_model"),
            "device": _value(settings, "device"),
            "source_language": _value(settings, "source_language"),
            "vad_filter": _value(settings, "vad_filter"),
            "openai_transcribe_model": _value(settings, "openai_transcribe_model"),
            "openai_chunk_s": _value(settings, "openai_chunk_s"),
            "aligner": _value(settings, "aligner"),
            "alignment_device": _value(settings, "alignment_device"),
            "timecode_quality": _value(settings, "timecode_quality"),
            "max_segment_s": _value(settings, "max_segment_s"),
            "merge_gap_s": _value(settings, "merge_gap_s"),
            "drop_non_korean_intro_s": _value(settings, "drop_non_korean_intro_s"),
            "manual_transcript_hash": file_hash(Path(transcript_input)) if transcript_input else None,
            "transcript_pipeline": "aligned-base-v2",
        }
    )


def correction_cache_key(aligned_hash: str, settings: Mapping[str, Any] | object) -> str:
    glossary = _value(settings, "glossary")
    return stable_hash(
        {
            "aligned_hash": aligned_hash,
            "mode": _value(settings, "transcript_correction"),
            "model": _value(settings, "correction_model"),
            "glossary_hash": file_hash(Path(glossary)) if glossary else None,
            "correction_pipeline": "transcript-correction-v1",
        }
    )


def translation_cache_key(transcript_hash: str, settings: Mapping[str, Any] | object) -> str:
    return stable_hash(
        {
            "transcript_hash": transcript_hash,
            "translate_mode": _value(settings, "translate_mode"),
            "translate_model": _value(settings, "translate_model"),
            "translation_required": bool(_value(settings, "translation_required", False)),
            "translation_min_success_ratio": _value(settings, "translation_min_success_ratio", 0.0),
            "translation_pipeline": "segment-stable-v1",
        }
    )


def vision_cache_key(
    *,
    input_hash: str,
    translated_hash: str,
    video_profile_hash: str | None,
    settings: Mapping[str, Any] | object,
) -> str:
    return stable_hash(
        {
            "input_hash": input_hash,
            "translated_hash": translated_hash,
            "video_profile_hash": video_profile_hash,
            "gap_threshold": _value(settings, "gap_threshold"),
            "max_vision_frames": _value(settings, "max_vision_frames"),
            "max_visual_gap_s": _value(settings, "max_visual_gap_s"),
            "vision_provider": _value(settings, "vision_provider", "openai"),
            "vision_model": _value(settings, "vision_model"),
            "vision_resize_long_edge": _value(settings, "vision_resize_long_edge", 768),
            "vision_batch_size": _value(settings, "vision_batch_size", 1),
            "drop_visual_before_s": _value(settings, "drop_visual_before_s"),
            "vision_pipeline": "silent-gap-profile-v2",
        }
    )
