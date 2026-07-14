from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.schema import (
    CommentaryAudio,
    CommentaryScript,
    ReactionAudioStream,
    ReactionBlocks,
    ReactionCutPoint,
    ReactionSource,
    ReactionSubtitleStream,
    ReactionVideoStream,
    RemixEdl,
    RemixPlan,
    RemixQa,
    RemixQaCommentary,
    RemixQaReactionPreservation,
    RemixQaTimeline,
    validate_commentary_audio,
    validate_commentary_script,
    validate_reaction_source,
    validate_remix_edl,
    validate_remix_plan,
    validate_remix_qa,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONTRACT_EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "reaction-remix" / "examples" / "contracts"


def make_source() -> ReactionSource:
    return ReactionSource(
        input_path="C:/source.mp4",
        input_hash="a" * 64,
        duration_s=10.0,
        video=ReactionVideoStream(
            stream_index=0,
            codec="h264",
            width=1920,
            height=1080,
            fps_num=30000,
            fps_den=1001,
            pixel_format="yuv420p",
            frame_rate_mode="cfr",
        ),
        audio=ReactionAudioStream(
            stream_index=1,
            codec="aac",
            sample_rate=44100,
            channels=2,
            channel_layout="stereo",
        ),
        has_burned_in_subtitles=True,
        created_at=NOW,
        config_hash="b" * 64,
    )


def test_reaction_hash_is_plain_64_hex() -> None:
    payload = make_source().model_dump(mode="json")
    payload["input_hash"] = "sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="64 lowercase hexadecimal"):
        ReactionSource.model_validate(payload)


def test_soft_subtitle_stream_fails_v1_validation() -> None:
    source = make_source().model_copy(
        update={
            "subtitle_streams": [
                ReactionSubtitleStream(stream_index=2, codec="ass", language="ja")
            ]
        }
    )
    with pytest.raises(ValueError, match="soft subtitle"):
        validate_reaction_source(source)


def test_reaction_contract_rejects_backslash_paths() -> None:
    payload = make_source().model_dump(mode="json")
    payload["input_path"] = r"C:\source.mp4"
    with pytest.raises(ValidationError, match="forward slashes"):
        ReactionSource.model_validate(payload)


def test_reaction_contract_examples_match_runtime_schema() -> None:
    source = ReactionSource.model_validate_json((CONTRACT_EXAMPLES / "reaction_source.json").read_text(encoding="utf-8"))
    blocks = ReactionBlocks.model_validate_json((CONTRACT_EXAMPLES / "reaction_blocks.json").read_text(encoding="utf-8"))
    plan = RemixPlan.model_validate_json((CONTRACT_EXAMPLES / "remix_plan.json").read_text(encoding="utf-8"))
    script = CommentaryScript.model_validate_json(
        (CONTRACT_EXAMPLES / "commentary_script.json").read_text(encoding="utf-8")
    )
    audio = CommentaryAudio.model_validate_json(
        (CONTRACT_EXAMPLES / "commentary_audio.json").read_text(encoding="utf-8")
    )
    edl = RemixEdl.model_validate_json((CONTRACT_EXAMPLES / "remix_edl.json").read_text(encoding="utf-8"))
    qa = RemixQa.model_validate_json((CONTRACT_EXAMPLES / "remix_qa.json").read_text(encoding="utf-8"))

    validate_reaction_source(source)
    validate_remix_plan(plan, blocks)
    validate_commentary_script(script, plan)
    validate_commentary_audio(audio, script)
    validate_remix_edl(edl, source, audio)
    validate_remix_qa(qa)


def test_reaction_cut_point_safety_is_additive_and_validated() -> None:
    legacy = ReactionCutPoint(
        cut_point_id="cut-legacy",
        tc=1.0,
        kind="turn_boundary",
        confidence=0.89,
        speech_padding_s=0.12,
    )
    assert legacy.safety_mode is None

    protected_edge = ReactionCutPoint(
        cut_point_id="cut-protected-edge",
        tc=1.05,
        kind="turn_boundary",
        confidence=0.89,
        speech_padding_s=0.12,
        safety_mode=None,
        left_handle_s=0.05,
        right_handle_s=0.10,
    )
    assert protected_edge.safety_mode is None
    assert protected_edge.left_handle_s == 0.05

    with pytest.raises(ValidationError, match="cannot exceed 0.89"):
        ReactionCutPoint.model_validate(protected_edge.model_dump() | {"confidence": 0.90})

    word_edge = ReactionCutPoint(
        cut_point_id="cut-word-edge",
        tc=1.1,
        kind="turn_boundary",
        confidence=0.90,
        speech_padding_s=0.12,
        safety_mode="word_edge",
        left_handle_s=0.05,
        right_handle_s=0.05,
    )
    assert word_edge.confidence == 0.90

    with pytest.raises(ValidationError, match="must equal 0.90"):
        ReactionCutPoint.model_validate(word_edge.model_dump() | {"confidence": 0.91})
    with pytest.raises(ValidationError, match="must equal 0.90"):
        ReactionCutPoint.model_validate(word_edge.model_dump() | {"confidence": 0.89})


def test_qa_timeline_decoded_count_deltas_are_self_consistent() -> None:
    with pytest.raises(ValidationError, match="frame_count_delta"):
        RemixQaTimeline(
            gap_count=0,
            overlap_count=0,
            decode_ok=True,
            expected_frame_count=100,
            actual_frame_count=102,
            frame_count_delta=1,
            expected_sample_count=1000,
            actual_sample_count=1000,
            sample_count_delta=0,
            status="fail",
        )


def test_qa_commentary_protected_overlap_ids_are_additive_and_unique() -> None:
    legacy = RemixQaCommentary(
        slots_checked=0,
        provider_mismatches=0,
        voice_mismatches=0,
        old_narrator_leakage_count=0,
        min_asr_text_match=1.0,
        status="pass",
    )
    assert legacy.protected_narrator_overlap_block_ids == []

    with pytest.raises(ValidationError, match="protected narrator overlap block IDs must be unique"):
        RemixQaCommentary(
            slots_checked=0,
            provider_mismatches=0,
            voice_mismatches=0,
            old_narrator_leakage_count=0,
            protected_narrator_overlap_block_ids=["block-0002", "block-0002"],
            min_asr_text_match=1.0,
            status="pass",
        )


def test_qa_reaction_failed_placement_ids_are_additive_and_unique() -> None:
    legacy = RemixQaReactionPreservation(
        placements_checked=1,
        speed_mismatches=0,
        gain_mismatches=0,
        span_mismatches=0,
        max_gain_delta_db=0.0,
        min_audio_correlation=1.0,
        max_av_drift_ms=0.0,
        min_sample_frame_similarity=1.0,
        status="pass",
    )
    assert legacy.failed_placement_ids == []

    with pytest.raises(ValidationError, match="failed reaction preservation placement IDs must be unique"):
        RemixQaReactionPreservation(
            placements_checked=2,
            speed_mismatches=0,
            gain_mismatches=0,
            span_mismatches=0,
            failed_placement_ids=["placement-0000", "placement-0000"],
            max_gain_delta_db=0.4,
            min_audio_correlation=0.9,
            max_av_drift_ms=0.0,
            min_sample_frame_similarity=1.0,
            status="fail",
        )
