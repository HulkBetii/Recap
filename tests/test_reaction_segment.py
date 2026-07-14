from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.integrity import file_hash
from common.schema import (
    ReactionAnalysisRegion,
    ReactionAsrInfo,
    ReactionAudioStream,
    ReactionSource,
    ReactionSpeakerCluster,
    ReactionTranscript,
    ReactionTurn,
    ReactionVideoStream,
    ReactionWord,
)
from reaction_remix.segment.blocks import SegmentSettings, build_reaction_blocks
from reaction_remix.segment.cut_points import select_cut
from reaction_remix.segment.__main__ import run_segment
from reaction_remix.segment.review_html import write_blocks_review_html


def source_and_transcript() -> tuple[ReactionSource, ReactionTranscript]:
    now = datetime.now(timezone.utc)
    source = ReactionSource(
        input_path="C:/source.mp4",
        input_hash="a" * 64,
        duration_s=10.0,
        video=ReactionVideoStream(
            stream_index=0,
            codec="h264",
            width=1920,
            height=1080,
            fps_num=30,
            fps_den=1,
            pixel_format="yuv420p",
            frame_rate_mode="cfr",
        ),
        audio=ReactionAudioStream(
            stream_index=1,
            codec="aac",
            sample_rate=48000,
            channels=2,
            channel_layout="stereo",
        ),
        has_burned_in_subtitles=True,
        created_at=now,
        config_hash="b" * 64,
    )
    turns = [
        ReactionTurn(
            turn_id=0,
            tc_start=1.0,
            tc_end=2.0,
            text="I miss Japan.",
            language="en",
            language_confidence=0.99,
            speaker_id="reactor",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id="region-0001",
            words=[ReactionWord(word_id="word-0", tc_start=1.0, tc_end=2.0, text="reaction", confidence=0.99)],
        ),
        ReactionTurn(
            turn_id=1,
            tc_start=4.0,
            tc_end=5.0,
            text="次の反応を見てみよう。",
            language="ja",
            language_confidence=0.99,
            speaker_id="narrator",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id="region-0002",
            words=[ReactionWord(word_id="word-1", tc_start=4.0, tc_end=5.0, text="ナレーション", confidence=0.99)],
        ),
    ]
    transcript = ReactionTranscript(
        source_hash=source.input_hash,
        source_duration_s=source.duration_s,
        regions=[
            ReactionAnalysisRegion(region_id="region-0001", tc_start=0.0, tc_end=3.0, status="ok", attempts=1),
            ReactionAnalysisRegion(region_id="region-0002", tc_start=3.0, tc_end=6.0, status="ok", attempts=1),
            ReactionAnalysisRegion(
                region_id="region-0003",
                tc_start=6.0,
                tc_end=8.0,
                status="analysis_gap",
                attempts=2,
                error="ASR failed",
            ),
        ],
        turns=turns,
        speaker_clusters=[
            ReactionSpeakerCluster(
                speaker_id="reactor",
                region_count=1,
                total_duration_s=1.0,
                language_ratios={"en": 1.0},
                confidence=0.99,
            ),
            ReactionSpeakerCluster(
                speaker_id="narrator",
                region_count=3,
                total_duration_s=8.0,
                language_ratios={"ja": 1.0},
                narrator_candidate=True,
                confidence=0.99,
            ),
        ],
        narrator_speaker_id="narrator",
        asr=ReactionAsrInfo(device="cuda", chunk_s=30.0, overlap_s=2.0),
        created_at=now,
    )
    return source, transcript


def test_segment_covers_timeline_and_preserves_analysis_gap() -> None:
    source, transcript = source_and_transcript()
    blocks = build_reaction_blocks(source, transcript, settings=SegmentSettings())
    assert blocks.blocks[0].tc_start == 0.0
    assert blocks.blocks[-1].tc_end == 10.0
    assert all(
        abs(current.tc_start - previous.tc_end) < 1e-6
        for previous, current in zip(blocks.blocks, blocks.blocks[1:])
    )
    assert any(block.kind == "commentary" for block in blocks.blocks)
    assert any(block.kind == "unknown" and block.tc_start <= 6.0 and block.tc_end >= 8.0 for block in blocks.blocks)
    assert all(block.block_id == f"block-{index:04d}" for index, block in enumerate(blocks.blocks, start=1))


def test_turn_cut_respects_speech_padding_and_never_cuts_a_word() -> None:
    words = [ReactionWord(word_id="word-cut", tc_start=1.10, tc_end=1.30, text="word", confidence=0.99)]
    cut = select_cut(
        1.0,
        1.5,
        scene_boundaries=[1.2, 1.38],
        words=words,
        scene_window_s=0.5,
        min_silence_s=1.0,
        speech_padding_s=0.12,
        boundary_confidence=0.92,
        boundary_policy="strict",
    )

    assert cut.tc == 1.38
    assert cut.tc >= 1.12
    assert cut.tc <= 1.38
    assert not (words[0].tc_start < cut.tc < words[0].tc_end)


def test_non_narrator_boundary_without_full_padding_is_not_labeled_word_edge() -> None:
    words = [
        ReactionWord(word_id="word-left", tc_start=1.0, tc_end=4.05, text="reaction", confidence=0.99),
        ReactionWord(word_id="word-right", tc_start=4.20, tc_end=5.0, text="commentary", confidence=0.99),
    ]
    cut = select_cut(
        4.05,
        4.20,
        scene_boundaries=[4.12],
        words=words,
        scene_window_s=0.5,
        min_silence_s=0.25,
        speech_padding_s=0.12,
        boundary_confidence=0.92,
        boundary_policy="strict",
    )

    assert cut.tc == 4.12
    assert cut.safety_mode is None
    assert cut.confidence == 0.89
    assert cut.left_handle_s == pytest.approx(0.07)
    assert cut.right_handle_s == pytest.approx(0.08)


def test_adjacent_turns_without_full_padding_downgrade_commentary() -> None:
    source, transcript = source_and_transcript()
    transcript.turns[0] = transcript.turns[0].model_copy(
        update={
            "tc_end": 4.05,
            "words": [
                transcript.turns[0].words[0].model_copy(update={"tc_end": 4.05})
            ],
        }
    )
    transcript.turns[1] = transcript.turns[1].model_copy(
        update={
            "tc_start": 4.20,
            "words": [
                transcript.turns[1].words[0].model_copy(update={"tc_start": 4.20})
            ],
        }
    )

    blocks = build_reaction_blocks(
        source,
        transcript,
        scene_boundaries=[4.12],
        settings=SegmentSettings(boundary_policy="strict"),
    )
    commentary_candidate = next(block for block in blocks.blocks if 1 in block.turn_ids)

    boundary = next(
        cut for cut in blocks.cut_points if cut.cut_point_id == commentary_candidate.start_cut_point_id
    )
    assert commentary_candidate.boundary_confidence == 0.89
    assert commentary_candidate.kind == "mixed"
    assert boundary.safety_mode is None
    assert "configured policy" in commentary_candidate.warnings[0]


def test_non_narrator_protected_edge_keeps_adjacent_reaction_events_separate() -> None:
    source, transcript = source_and_transcript()
    transcript = transcript.model_copy(
        update={
            "narrator_speaker_id": None,
            "regions": [],
            "turns": [
                transcript.turns[0].model_copy(
                    update={
                        "tc_end": 4.05,
                        "words": [transcript.turns[0].words[0].model_copy(update={"tc_end": 4.05})],
                    }
                ),
                transcript.turns[1].model_copy(
                    update={
                        "tc_start": 4.20,
                        "language": "en",
                        "speaker_id": "reactor-two",
                        "words": [transcript.turns[1].words[0].model_copy(update={"tc_start": 4.20})],
                    }
                ),
            ],
        }
    )

    blocks = build_reaction_blocks(
        source,
        transcript,
        scene_boundaries=[4.12],
        settings=SegmentSettings(boundary_policy="strict"),
    )

    first = next(block for block in blocks.blocks if block.turn_ids == [0])
    second = next(block for block in blocks.blocks if block.turn_ids == [1])
    boundary = next(cut for cut in blocks.cut_points if cut.cut_point_id == first.end_cut_point_id)
    assert first.kind == "reaction"
    assert second.kind == "reaction"
    assert first.tc_end == second.tc_start == pytest.approx(4.12)
    assert boundary.safety_mode is None
    assert boundary.confidence == 0.89


def test_word_edge_policy_keeps_high_confidence_japanese_narrator() -> None:
    source, transcript = source_and_transcript()
    transcript.turns[0] = transcript.turns[0].model_copy(
        update={
            "tc_end": 4.05,
            "words": [transcript.turns[0].words[0].model_copy(update={"tc_end": 4.05})],
        }
    )
    transcript.turns[1] = transcript.turns[1].model_copy(
        update={
            "tc_start": 4.20,
            "words": [transcript.turns[1].words[0].model_copy(update={"tc_start": 4.20})],
        }
    )

    blocks = build_reaction_blocks(source, transcript, scene_boundaries=[4.12])
    commentary = next(block for block in blocks.blocks if 1 in block.turn_ids)
    boundary = next(cut for cut in blocks.cut_points if cut.cut_point_id == commentary.start_cut_point_id)

    assert commentary.kind == "commentary"
    assert commentary.boundary_confidence == 0.90
    assert boundary.safety_mode == "word_edge"
    assert boundary.confidence == 0.90
    assert boundary.left_handle_s == pytest.approx(0.07)
    assert boundary.right_handle_s == pytest.approx(0.08)


def test_content_overlap_never_receives_word_edge_confidence() -> None:
    cut = select_cut(
        4.30,
        4.20,
        scene_boundaries=[],
        words=[],
        scene_window_s=0.5,
        min_silence_s=0.25,
        speech_padding_s=0.12,
        boundary_confidence=0.92,
        boundary_policy="strict-or-word-edge",
        word_edge_eligible=True,
    )

    assert cut.safety_mode == "overlap"
    assert cut.confidence <= 0.89


def test_overlapping_speech_is_preserved_as_low_confidence_mixed() -> None:
    source, transcript = source_and_transcript()
    transcript.turns[0] = transcript.turns[0].model_copy(
        update={
            "tc_end": 4.30,
            "words": [transcript.turns[0].words[0].model_copy(update={"tc_end": 4.30})],
        }
    )
    transcript.turns[1] = transcript.turns[1].model_copy(
        update={
            "tc_start": 4.20,
            "words": [transcript.turns[1].words[0].model_copy(update={"tc_start": 4.20})],
        }
    )

    blocks = build_reaction_blocks(source, transcript)
    overlap = next(block for block in blocks.blocks if block.turn_ids == [0, 1])

    assert overlap.kind == "mixed"
    assert overlap.boundary_confidence <= 0.89


def test_source_boundary_narrator_is_protected_instead_of_promoted() -> None:
    source, transcript = source_and_transcript()
    narrator = transcript.turns[1]
    for start, end in ((0.0, 1.0), (9.0, 10.0)):
        word = narrator.words[0].model_copy(update={"tc_start": start, "tc_end": end})
        candidate = transcript.model_copy(
            update={
                "regions": [],
                "turns": [narrator.model_copy(update={"tc_start": start, "tc_end": end, "words": [word]})],
            }
        )
        blocks = build_reaction_blocks(source, candidate)
        protected = next(block for block in blocks.blocks if block.turn_ids == [1])
        boundary_id = protected.start_cut_point_id if start == 0.0 else protected.end_cut_point_id
        boundary = next(cut for cut in blocks.cut_points if cut.cut_point_id == boundary_id)

        assert protected.kind == "mixed"
        assert protected.preservation.audio == "source_mix"
        assert not protected.eligible_commentary_visual
        assert boundary.safety_mode == "source_boundary"
        assert boundary.confidence == 1.0


def test_tiny_effective_speech_block_is_merged_after_safe_cut_selection() -> None:
    source, transcript = source_and_transcript()
    transcript = transcript.model_copy(
        update={
            "narrator_speaker_id": None,
            "turns": [
                ReactionTurn(
                    turn_id=0,
                    tc_start=1.0,
                    tc_end=4.0,
                    text="reaction one",
                    language="en",
                    language_confidence=0.99,
                    speaker_id="reactor-a",
                    speaker_confidence=0.99,
                    asr_confidence=0.99,
                    region_id="region-0001",
                    words=[ReactionWord(word_id="word-a", tc_start=1.0, tc_end=4.0, text="reaction", confidence=0.99)],
                ),
                ReactionTurn(
                    turn_id=1,
                    tc_start=4.0,
                    tc_end=4.16,
                    text="I",
                    language="und",
                    language_confidence=0.03,
                    speaker_id="speaker-unknown",
                    speaker_confidence=0.99,
                    asr_confidence=0.82,
                    region_id="region-0002",
                    words=[ReactionWord(word_id="word-tiny", tc_start=4.152, tc_end=4.16, text="I", confidence=0.82)],
                ),
                ReactionTurn(
                    turn_id=2,
                    tc_start=4.16,
                    tc_end=5.0,
                    text="reaction two",
                    language="en",
                    language_confidence=0.99,
                    speaker_id="reactor-b",
                    speaker_confidence=0.99,
                    asr_confidence=0.99,
                    region_id="region-0002",
                    words=[ReactionWord(word_id="word-b", tc_start=4.16, tc_end=5.0, text="reaction", confidence=0.99)],
                ),
            ],
        }
    )

    blocks = build_reaction_blocks(source, transcript, scene_boundaries=[4.131])

    assert all(
        current.tc - previous.tc >= 0.08 - 1e-6
        for previous, current in zip(blocks.cut_points, blocks.cut_points[1:])
    )
    assert any(1 in block.turn_ids and block.kind == "unknown" for block in blocks.blocks)


def test_configured_boundary_confidence_downgrades_commentary() -> None:
    source, transcript = source_and_transcript()
    blocks = build_reaction_blocks(
        source,
        transcript,
        settings=SegmentSettings(commentary_min_confidence=0.95),
    )
    assert not any(block.kind == "commentary" for block in blocks.blocks)
    assert any(block.kind == "mixed" for block in blocks.blocks)


def test_asr_confidence_is_not_a_commentary_role_signal() -> None:
    source, transcript = source_and_transcript()
    transcript.turns[1] = transcript.turns[1].model_copy(update={"asr_confidence": 0.80})
    blocks = build_reaction_blocks(source, transcript)
    assert any(block.kind == "commentary" for block in blocks.blocks)


def test_adjacent_reaction_boundary_trims_commentary_not_reaction_speech() -> None:
    source, transcript = source_and_transcript()
    transcript.turns[0] = transcript.turns[0].model_copy(update={"tc_end": 4.0})
    transcript.turns[1] = transcript.turns[1].model_copy(
        update={
            "words": [
                transcript.turns[1].words[0].model_copy(update={"tc_start": 4.2})
            ]
        }
    )
    blocks = build_reaction_blocks(source, transcript)
    reaction = next(block for block in blocks.blocks if 0 in block.turn_ids)
    commentary = next(block for block in blocks.blocks if 1 in block.turn_ids)

    assert reaction.tc_end <= 4.2
    assert reaction.tc_end >= 2.0
    assert commentary.tc_start == reaction.tc_end
    assert commentary.kind == "commentary"


def test_block_ids_and_spans_do_not_encode_classification() -> None:
    source, transcript = source_and_transcript()
    confident = build_reaction_blocks(source, transcript)
    transcript.turns[1] = transcript.turns[1].model_copy(update={"speaker_confidence": 0.80})
    conservative = build_reaction_blocks(source, transcript)
    assert [
        (block.block_id, block.tc_start, block.tc_end)
        for block in confident.blocks
    ] == [
        (block.block_id, block.tc_start, block.tc_end)
        for block in conservative.blocks
    ]


def test_review_html_contains_audit_fields(tmp_path: Path) -> None:
    source, transcript = source_and_transcript()
    blocks = build_reaction_blocks(source, transcript)
    output = tmp_path / "reaction_blocks.review.html"
    write_blocks_review_html(output, blocks, transcript)
    text = output.read_text(encoding="utf-8")
    assert "Reaction Blocks Audit" in text
    assert "boundary" in text
    assert "full_handle" in text
    assert "L " in text
    assert "block-0001" in text


def test_review_html_labels_protected_insufficient_handle_edges(tmp_path: Path) -> None:
    source, transcript = source_and_transcript()
    transcript.turns[0] = transcript.turns[0].model_copy(
        update={
            "tc_end": 4.05,
            "words": [transcript.turns[0].words[0].model_copy(update={"tc_end": 4.05})],
        }
    )
    transcript.turns[1] = transcript.turns[1].model_copy(
        update={
            "tc_start": 4.20,
            "words": [transcript.turns[1].words[0].model_copy(update={"tc_start": 4.20})],
        }
    )
    blocks = build_reaction_blocks(
        source,
        transcript,
        scene_boundaries=[4.12],
        settings=SegmentSettings(boundary_policy="strict"),
    )
    output = tmp_path / "reaction_blocks.review.html"
    write_blocks_review_html(output, blocks, transcript)

    assert "insufficient_handle / protected edge" in output.read_text(encoding="utf-8")


def test_segment_cli_records_transcript_file_hash(tmp_path: Path) -> None:
    source, transcript = source_and_transcript()
    source_path = tmp_path / "reaction_source.json"
    transcript_path = tmp_path / "reaction_transcript.json"
    output_path = tmp_path / "reaction_blocks.json"
    source_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")
    transcript_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
    args = SimpleNamespace(
        source=source_path,
        transcript=transcript_path,
        shots=None,
        output=output_path,
        review_html=tmp_path / "reaction_blocks.review.html",
        work_dir=tmp_path / "work",
        min_silence_s=0.25,
        speech_padding_s=0.12,
        scene_window_s=0.5,
        min_cut_spacing_s=0.08,
        commentary_min_confidence=0.90,
        narrator_min_regions=3,
        narrator_min_japanese_ratio=0.90,
        broll_gap_s=1.5,
        boundary_policy="strict-or-word-edge",
        force=True,
    )
    assert run_segment(args) == 0
    assert file_hash(transcript_path) is not None
    payload = output_path.read_text(encoding="utf-8")
    assert f'"transcript_hash": "{file_hash(transcript_path)}"' in payload
    meta_path = output_path.with_name("reaction_blocks.meta.json")
    first_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert first_meta["algorithm_version"] == "reaction-segment-v7"

    args.force = False
    args.boundary_policy = "strict"
    assert run_segment(args) == 0
    second_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert second_meta["config_hash"] != first_meta["config_hash"]
