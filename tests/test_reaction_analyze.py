from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from common.schema import (
    ReactionAudioStream,
    ReactionSource,
    ReactionSpeakerCluster,
    ReactionTurn,
    ReactionVideoStream,
)
from reaction_remix.analyze.asr import RawTurn, RawWord
from reaction_remix.analyze.core import AnalyzeSettings, _prefer_refined_turns, _uncovered_spans, analyze_reaction
from reaction_remix.analyze.regions import TimeSpan, split_analysis_regions, split_at_silence_midpoints
from reaction_remix.analyze.speakers import _speaker_units, select_narrator_speaker


class FailedTranscriber:
    def transcribe(self, _audio_path: Path):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic ASR failure")


class NoopLanguageVerifier:
    def detect(self, _text: str) -> tuple[str, float]:
        return "und", 0.0


class NoopSpeakerClusterer:
    def cluster(self, _audio_path: Path, _turns: list[ReactionTurn]) -> dict[int, tuple[str, float]]:
        return {}


def make_source(path: Path, duration_s: float = 10.0) -> ReactionSource:
    return ReactionSource(
        input_path=path.resolve().as_posix(),
        input_hash="a" * 64,
        duration_s=duration_s,
        video=ReactionVideoStream(
            stream_index=0,
            codec="h264",
            width=1280,
            height=720,
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
        created_at=datetime.now(timezone.utc),
        config_hash="b" * 64,
    )


def test_region_split_respects_max_duration_and_overlap() -> None:
    regions = split_analysis_regions([TimeSpan(0.0, 65.0)], max_region_s=30.0, overlap_s=2.0)
    assert regions == [TimeSpan(0.0, 30.0), TimeSpan(28.0, 58.0), TimeSpan(56.0, 65.0)]


def test_analysis_regions_prefer_silence_midpoints_and_keep_thirty_second_cap() -> None:
    regions = split_at_silence_midpoints(
        70.0,
        [TimeSpan(9.0, 11.0), TimeSpan(43.0, 45.0)],
        max_region_s=30.0,
        overlap_s=2.0,
    )

    assert any(region.tc_end == 10.0 for region in regions)
    assert any(region.tc_start == 10.0 for region in regions)
    assert any(region.tc_end == 44.0 for region in regions)
    assert any(region.tc_start == 44.0 for region in regions)
    assert all(region.duration_s <= 30.0 + 1e-6 for region in regions)
    assert regions[0].tc_start == 0.0
    assert regions[-1].tc_end == 70.0


def test_failed_region_becomes_analysis_gap(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "source.mp4"
    input_path.write_bytes(b"media")

    def fake_extract(_input: Path, output: Path, _span: TimeSpan) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wav")

    monkeypatch.setattr("reaction_remix.analyze.core.extract_region_audio", fake_extract)
    transcript, warnings = analyze_reaction(
        input_path,
        make_source(input_path),
        tmp_path / "work",
        settings=AnalyzeSettings(max_attempts=2),
        transcriber=FailedTranscriber(),
        language_verifier=NoopLanguageVerifier(),
        speaker_clusterer=NoopSpeakerClusterer(),
        silence_spans=[],
    )
    assert transcript.turns == []
    assert transcript.regions[0].status == "analysis_gap"
    assert transcript.regions[0].attempts == 2
    assert "synthetic ASR failure" in warnings[0]


def test_narrator_requires_japanese_interleaved_regions() -> None:
    turns = [
        ReactionTurn(
            turn_id=index,
            tc_start=float(index * 3),
            tc_end=float(index * 3 + 2),
            text="日本語" if index % 2 == 0 else "reaction",
            language="ja" if index % 2 == 0 else "en",
            language_confidence=0.99,
            speaker_id="narrator" if index % 2 == 0 else "reactor",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id=f"region-{index}",
        )
        for index in range(7)
    ]
    clusters = [
        ReactionSpeakerCluster(
            speaker_id="narrator",
            region_count=4,
            total_duration_s=8.0,
            language_ratios={"ja": 1.0},
            confidence=0.99,
        ),
        ReactionSpeakerCluster(
            speaker_id="reactor",
            region_count=3,
            total_duration_s=6.0,
            language_ratios={"en": 1.0},
            confidence=0.99,
        ),
    ]
    narrator, updated = select_narrator_speaker(turns, clusters, source_duration_s=100.0)
    assert narrator == "narrator"
    assert next(item for item in updated if item.speaker_id == "narrator").narrator_candidate is True


def test_uncovered_spans_feed_multilingual_second_pass() -> None:
    turns = [
        ("region-1", 0.0, RawTurn(0.0, 1.0, "English", "en", 0.9, 0.9)),
        ("region-1", 0.0, RawTurn(5.0, 6.0, "English", "en", 0.9, 0.9)),
    ]
    assert _uncovered_spans(turns, duration_s=6.0) == [TimeSpan(1.0, 5.0)]


def test_short_multilingual_windows_replace_overlapping_primary_turns() -> None:
    primary = [("region-1", 0.0, RawTurn(0.0, 6.0, "English guess", "en", 0.8, 0.7))]
    refined = [
        ("refine-1", 0.0, RawTurn(0.0, 3.0, "日本語の導入", "ja", 1.0, 0.9)),
        ("refine-2", 0.0, RawTurn(3.0, 6.0, "次を見てみよう", "ja", 1.0, 0.9)),
    ]

    selected = _prefer_refined_turns(primary, refined)

    assert [turn.language for _region, _offset, turn in selected] == ["ja", "ja"]


def test_partial_refinement_preserves_uncovered_primary_speech() -> None:
    primary = [
        (
            "region-1",
            0.0,
            RawTurn(
                0.0,
                6.0,
                "before one covered two after three",
                "en",
                0.9,
                0.8,
                words=[
                    RawWord(0.0, 1.0, "before", 0.9),
                    RawWord(1.0, 2.0, "one", 0.9),
                    RawWord(2.0, 3.0, "covered", 0.9),
                    RawWord(3.0, 4.0, "two", 0.9),
                    RawWord(4.0, 5.0, "after", 0.9),
                    RawWord(5.0, 6.0, "three", 0.9),
                ],
            ),
        )
    ]
    refined = [
        (
            "refine-1",
            0.0,
            RawTurn(
                2.0,
                4.0,
                "refined speech",
                "en",
                0.95,
                0.95,
                words=[
                    RawWord(2.0, 3.0, "refined", 0.95),
                    RawWord(3.0, 4.0, "speech", 0.95),
                ],
            ),
        )
    ]

    selected = _prefer_refined_turns(primary, refined)

    assert [
        (offset + turn.start, offset + turn.end, turn.text)
        for _region, offset, turn in selected
    ] == [
        (0.0, 2.0, "before one"),
        (2.0, 4.0, "refined speech"),
        (4.0, 6.0, "after three"),
    ]
    word_spans = sorted(
        (offset + word.start, offset + word.end)
        for _region, offset, turn in selected
        for word in turn.words
    )
    assert word_spans == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
        (3.0, 4.0),
        (4.0, 5.0),
        (5.0, 6.0),
    ]


def test_speaker_units_merge_japanese_context_but_split_latin_und() -> None:
    turns = [
        ReactionTurn(
            turn_id=0,
            tc_start=0.0,
            tc_end=1.0,
            text="日本の反応",
            language="ja",
            language_confidence=1.0,
            speaker_id="pending",
            speaker_confidence=0.0,
            asr_confidence=0.8,
            region_id="region-1",
        ),
        ReactionTurn(
            turn_id=1,
            tc_start=1.1,
            tc_end=2.0,
            text="次を見よう",
            language="ja",
            language_confidence=1.0,
            speaker_id="pending",
            speaker_confidence=0.0,
            asr_confidence=0.8,
            region_id="region-1",
        ),
        ReactionTurn(
            turn_id=2,
            tc_start=2.1,
            tc_end=3.0,
            text="I miss Japan",
            language="und",
            language_confidence=0.4,
            speaker_id="pending",
            speaker_confidence=0.0,
            asr_confidence=0.8,
            region_id="region-1",
        ),
    ]
    units = _speaker_units(turns)
    assert [unit.turn_ids for unit in units] == [[0, 1], [2]]
