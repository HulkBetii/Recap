from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from common.schema import CommentaryAudio, CommentaryScript, ReactionBlocks, ReactionSource, ReactionTranscript, RemixPlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64


def make_source(*, duration_s: float = 100.0, input_path: str = "C:/source.mp4", input_hash: str = HASH_A) -> ReactionSource:
    return ReactionSource.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "input_path": input_path,
            "input_hash": input_hash,
            "duration_s": duration_s,
            "video": {
                "stream_index": 0,
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps_num": 30000,
                "fps_den": 1001,
                "pixel_format": "yuv420p",
                "frame_rate_mode": "cfr",
            },
            "audio": {
                "stream_index": 1,
                "codec": "aac",
                "sample_rate": 44100,
                "channels": 2,
                "channel_layout": "stereo",
            },
            "subtitle_streams": [],
            "has_burned_in_subtitles": True,
            "subtitle_policy": "burned_in_preserve",
            "created_at": NOW,
            "config_hash": HASH_B,
            "warnings": [],
        }
    )


def make_transcript(*, source_hash: str = HASH_A) -> ReactionTranscript:
    return ReactionTranscript.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "source_duration_s": 100.0,
            "regions": [
                {"region_id": "region-0000", "tc_start": 0.0, "tc_end": 80.0, "status": "ok", "attempts": 1, "error": None, "warnings": []},
                {"region_id": "region-0001", "tc_start": 80.0, "tc_end": 100.0, "status": "ok", "attempts": 1, "error": None, "warnings": []},
            ],
            "turns": [
                {"turn_id": 0, "tc_start": 0.1, "tc_end": 79.9, "text": "A complete reaction", "language": "en", "language_confidence": 0.99, "speaker_id": "speaker-0000", "speaker_confidence": 0.99, "asr_confidence": 0.99, "region_id": "region-0000", "words": [], "warnings": []},
                {"turn_id": 1, "tc_start": 80.1, "tc_end": 99.9, "text": "古いナレーション", "language": "ja", "language_confidence": 0.99, "speaker_id": "speaker-narrator", "speaker_confidence": 0.99, "asr_confidence": 0.99, "region_id": "region-0001", "words": [], "warnings": []},
            ],
            "speaker_clusters": [
                {"speaker_id": "speaker-0000", "region_count": 1, "total_duration_s": 79.8, "language_ratios": {"en": 1.0}, "narrator_candidate": False, "confidence": 0.99},
                {"speaker_id": "speaker-narrator", "region_count": 1, "total_duration_s": 19.8, "language_ratios": {"ja": 1.0}, "narrator_candidate": True, "confidence": 0.99},
            ],
            "narrator_speaker_id": "speaker-narrator",
            "asr": {"provider": "faster-whisper", "model": "large-v3", "device": "cpu", "chunk_s": 30.0, "overlap_s": 2.0, "language_mode": "auto", "word_timestamps": True},
            "created_at": NOW,
            "warnings": [],
        }
    )


def make_blocks(*, source_hash: str = HASH_A, transcript_hash: str = "c" * 64) -> ReactionBlocks:
    return ReactionBlocks.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "transcript_hash": transcript_hash,
            "source_duration_s": 100.0,
            "cut_points": [
                {"cut_point_id": "cut-0000", "tc": 0.0, "kind": "source_boundary", "confidence": 1.0, "speech_padding_s": 0.0},
                {"cut_point_id": "cut-0001", "tc": 80.0, "kind": "turn_boundary", "confidence": 1.0, "speech_padding_s": 0.12},
                {"cut_point_id": "cut-0002", "tc": 100.0, "kind": "source_boundary", "confidence": 1.0, "speech_padding_s": 0.0},
            ],
            "blocks": [
                {
                    "block_id": "block-0001",
                    "kind": "reaction",
                    "tc_start": 0.0,
                    "tc_end": 80.0,
                    "content_tc_start": 0.1,
                    "content_tc_end": 79.9,
                    "start_cut_point_id": "cut-0000",
                    "end_cut_point_id": "cut-0001",
                    "turn_ids": [0],
                    "language_codes": ["en"],
                    "speaker_ids": ["speaker-0000"],
                    "sequence_group_id": None,
                    "sequence_index": None,
                    "semantic": None,
                    "preservation": {"video": "source_frames", "audio": "source_mix", "speed": 1.0, "allow_trim_to_safe_cut_points": True},
                    "eligible_commentary_visual": False,
                    "classification_confidence": 0.99,
                    "language_confidence": 0.99,
                    "speaker_confidence": 0.99,
                    "boundary_confidence": 0.99,
                    "warnings": [],
                },
                {
                    "block_id": "block-0002",
                    "kind": "commentary",
                    "tc_start": 80.0,
                    "tc_end": 100.0,
                    "content_tc_start": 80.1,
                    "content_tc_end": 99.9,
                    "start_cut_point_id": "cut-0001",
                    "end_cut_point_id": "cut-0002",
                    "turn_ids": [1],
                    "language_codes": ["ja"],
                    "speaker_ids": ["speaker-narrator"],
                    "sequence_group_id": None,
                    "sequence_index": None,
                    "semantic": None,
                    "preservation": {"video": "source_frames", "audio": "replace_commentary", "speed": 1.0, "allow_trim_to_safe_cut_points": True},
                    "eligible_commentary_visual": True,
                    "classification_confidence": 0.99,
                    "language_confidence": 0.99,
                    "speaker_confidence": 0.99,
                    "boundary_confidence": 0.99,
                    "warnings": [],
                },
            ],
            "created_at": NOW,
            "warnings": [],
        }
    )


def make_plan(*, source_hash: str = HASH_A, blocks_hash: str = "d" * 64) -> RemixPlan:
    return RemixPlan.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "blocks_hash": blocks_hash,
            "original_duration_s": 100.0,
            "duration_policy": {
                "hard_min_output_ratio": 0.8,
                "preferred_min_output_ratio": 0.85,
                "preferred_max_output_ratio": 0.9,
                "hard_max_output_ratio": 1.0,
                "target_duration_s": 85.0,
            },
            "items": [
                {
                    "item_id": "item-0000",
                    "order": 0,
                    "kind": "source_block",
                    "role": "hook",
                    "block_id": "block-0001",
                    "slot_id": None,
                    "start_cut_point_id": "cut-0000",
                    "end_cut_point_id": "cut-0001",
                    "evidence_block_ids": [],
                    "preferred_visual_block_ids": [],
                    "target_duration_s": None,
                    "max_duration_s": None,
                    "char_budget": None,
                    "dependency_group_id": None,
                    "reason": "Strong source reaction",
                },
                {
                    "item_id": "item-0001",
                    "order": 1,
                    "kind": "commentary_slot",
                    "role": "setup",
                    "block_id": None,
                    "slot_id": "slot-0001",
                    "start_cut_point_id": None,
                    "end_cut_point_id": None,
                    "evidence_block_ids": ["block-0001"],
                    "preferred_visual_block_ids": ["block-0002"],
                    "target_duration_s": 5.0,
                    "max_duration_s": 6.0,
                    "char_budget": 32,
                    "dependency_group_id": None,
                    "reason": "Bridge to the next topic",
                },
            ],
            "excluded_blocks": [
                {
                    "block_id": "block-0002",
                    "reason": "Original commentary is replaced by the new slot",
                    "category": "commentary",
                    "source_duration_s": 20.0,
                    "unique_reaction_speech_s": 0.0,
                }
            ],
            "semantic_annotations": [],
            "predicted_duration_s": 85.0,
            "predicted_output_ratio": 0.85,
            "retention": {
                "unique_reaction_speech_ratio": 1.0,
                "reaction_block_ratio": 1.0,
                "country_coverage_ratio": 1.0,
                "topic_coverage_ratio": 1.0,
            },
            "llm": {"backend": "chatgpt_playwright", "session_url": None, "attempts": 1},
            "created_at": NOW,
            "warnings": [],
        }
    )


def make_commentary_script(*, source_hash: str = HASH_A, plan_hash: str = "4" * 64) -> CommentaryScript:
    return CommentaryScript.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "plan_hash": plan_hash,
            "language": "ja",
            "style_id": "reaction-internet-ja-v1",
            "slots": [
                {
                    "slot_id": "slot-0001",
                    "before_item_id": "item-0000",
                    "after_item_id": None,
                    "role": "setup",
                    "text_ja": "新しい日本語コメントだ。",
                    "evidence_block_ids": ["block-0001"],
                    "target_duration_s": 5.0,
                    "max_duration_s": 6.0,
                    "char_budget": 32,
                    "tone_tags": ["humorous"],
                    "qa": {"language_ok": True, "evidence_ok": True, "style_ok": True, "length_ok": True},
                    "warnings": [],
                }
            ],
            "llm": {"backend": "chatgpt_playwright", "session_url": None, "attempts": 1},
            "created_at": NOW,
            "warnings": [],
        }
    )


def make_commentary_audio(
    audio_path: Path,
    *,
    source_hash: str = HASH_A,
    script_hash: str = "e" * 64,
    duration_s: float = 5.0,
) -> CommentaryAudio:
    return CommentaryAudio.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "script_hash": script_hash,
            "voice_policy": {
                "provider": "ai33",
                "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
                "model": "eleven_multilingual_v2",
                "speed": 1.0,
                "fallback_provider": None,
                "text_normalization": "ja_basic",
            },
            "items": [
                {
                    "slot_id": "slot-0001",
                    "audio_path": audio_path.as_posix(),
                    "duration_s": duration_s,
                    "provider": "ai33",
                    "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
                    "model": "eleven_multilingual_v2",
                    "speed": 1.0,
                    "text_hash": "f" * 64,
                    "cache_key": "1" * 64,
                    "normalized": True,
                    "lufs_i": -13.5,
                    "true_peak_dbfs": -1.7,
                    "asr_text_match": 0.96,
                    "warnings": [],
                }
            ],
            "total_commentary_duration_s": duration_s,
            "created_at": NOW,
            "warnings": [],
        }
    )
