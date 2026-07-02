from __future__ import annotations

from pathlib import Path

from common.schema import TranscriptSegment
from ingest.asr import (
    apply_alignment,
    detect_transcript_warnings,
    parse_markdown_transcript,
    parse_manual_transcript,
    split_long_segments,
    clean_aligned_segments,
)

SAMPLE = """# Transcript

- [02:00] 첫 번째 문장입니다. 다음 말입니다.
- [02:20] 두 번째 문장입니다.
- [02:40] 세 번째 문장입니다.
- [03:00] 가자!
- [04:00] 네 번째 장면입니다.
- [04:20] 다섯 번째 장면입니다.
- [04:40] 여섯 번째 장면입니다.
- [05:40] 마지막 장면입니다.
"""


def test_parse_markdown_transcript_infers_eight_ranges() -> None:
    segments = parse_markdown_transcript(SAMPLE, duration=360)
    assert len(segments) == 8
    assert segments[0].tc_start == 120
    assert segments[0].tc_end == 140
    assert segments[-1].tc_start == 340
    assert segments[-1].tc_end == 360


def test_parse_manual_transcript_marks_approximate(tmp_path: Path) -> None:
    path = tmp_path / "transcript.md"
    path.write_text(SAMPLE, encoding="utf-8")
    segments, quality = parse_manual_transcript(path, duration=360)
    assert len(segments) == 8
    assert quality.asr_provider == "manual"
    assert quality.approximate_timecodes is True
    assert quality.timecode_quality == "approximate"
    assert quality.warnings


def test_split_long_segments_monotonic_and_reassigns_ids() -> None:
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=90, ko="첫 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다.")
    result = split_long_segments([segment], max_segment_s=30)
    assert len(result) == 3
    assert [item.id for item in result] == [0, 1, 2]
    assert result[0].tc_start == 0
    assert result[-1].tc_end == 90
    assert all(result[index].tc_end <= result[index + 1].tc_start for index in range(len(result) - 1))


def test_detect_transcript_warnings_flags_chinese_hallucination() -> None:
    warnings = detect_transcript_warnings([
        TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="你好 你好 你好 你好 你好 你好 你好 你好 你好"),
    ])
    assert any("non-Korean" in warning for warning in warnings)


def test_unavailable_aligner_falls_back_to_approximate(tmp_path: Path) -> None:
    segments, quality = parse_manual_transcript(tmp_path / "missing.md", duration=1) if False else ([], None)
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="안녕")
    from common.schema import TranscriptQuality
    aligned, updated = apply_alignment([segment], TranscriptQuality(asr_provider="manual", timecode_quality="approximate", approximate_timecodes=True), "whisperx", "strict")
    assert aligned == [segment]
    assert updated.aligner_provider == "whisperx"
    assert updated.approximate_timecodes is True
    assert updated.warnings


def test_clean_aligned_segments_merges_short_and_clamps_duration() -> None:
    segments = [
        TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="? ?????."),
        TranscriptSegment(id=1, tc_start=1, tc_end=1.2, ko="??"),
        TranscriptSegment(id=2, tc_start=9, tc_end=12, ko="? ?????."),
    ]
    cleaned, warnings = clean_aligned_segments(segments, duration=10, min_segment_s=0.45, max_segment_s=30)
    assert len(cleaned) == 2
    assert cleaned[0].ko.endswith("??")
    assert cleaned[-1].tc_end == 10
    assert any("short segment merged" in warning for warning in warnings)


def test_clean_aligned_segments_flags_subtitle_artifact() -> None:
    segments = [TranscriptSegment(id=0, tc_start=0, tc_end=2, ko="???? by ???")]
    _cleaned, warnings = clean_aligned_segments(segments, duration=2, min_segment_s=0.45, max_segment_s=30)
    assert any("subtitle" in warning for warning in warnings)


def test_clean_aligned_segments_warns_when_transcript_ends_early() -> None:
    segments = [TranscriptSegment(id=0, tc_start=0, tc_end=2, ko="?????")]
    _cleaned, warnings = clean_aligned_segments(segments, duration=100, min_segment_s=0.45, max_segment_s=30)
    assert any("before media end" in warning for warning in warnings)
