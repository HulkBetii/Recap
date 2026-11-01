from __future__ import annotations

from pathlib import Path

from common.schema import TranscriptQuality, TranscriptSegment
from ingest.asr import apply_alignment, whisperx_language_code


def test_apply_alignment_uses_whisperx_success(monkeypatch, tmp_path: Path) -> None:
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=20, ko="안녕하세요 회장님.")
    quality = TranscriptQuality(asr_provider="openai-gpt4o-hybrid", timecode_quality="approximate", approximate_timecodes=True)

    def fake_align(segments, audio_path, device, source_language="ko"):  # type: ignore[no-untyped-def]
        assert device == "cuda"
        assert source_language == "ko"
        return [TranscriptSegment(id=0, tc_start=3.0, tc_end=5.0, ko=segments[0].ko)]

    monkeypatch.setattr("ingest.asr.align_with_whisperx", fake_align)
    aligned, updated = apply_alignment([segment], quality, "whisperx", "strict", audio_path=tmp_path / "a.mp3", alignment_device="cuda")
    assert aligned[0].tc_start == 3.0
    assert updated.timecode_quality == "strict"
    assert updated.approximate_timecodes is False
    assert updated.aligner_provider == "whisperx"


def test_apply_alignment_whisperx_failure_falls_back(monkeypatch, tmp_path: Path) -> None:
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=20, ko="안녕하세요")
    quality = TranscriptQuality(asr_provider="openai-gpt4o-hybrid", timecode_quality="approximate", approximate_timecodes=True)

    def fake_align(segments, audio_path, device, source_language="ko"):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr("ingest.asr.align_with_whisperx", fake_align)
    aligned, updated = apply_alignment([segment], quality, "whisperx", "strict", audio_path=tmp_path / "a.mp3")
    assert aligned == [segment]
    assert updated.timecode_quality == "approximate"
    assert updated.approximate_timecodes is True
    assert any("whisperx alignment failed" in warning for warning in updated.warnings)


def test_apply_alignment_passes_vietnamese_language_to_whisperx(monkeypatch, tmp_path: Path) -> None:
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=20, ko="Xin chào mọi người")
    quality = TranscriptQuality(asr_provider="openai-gpt4o-hybrid", timecode_quality="approximate", approximate_timecodes=True)
    seen = {}

    def fake_align(segments, audio_path, device, source_language="ko"):  # type: ignore[no-untyped-def]
        seen["source_language"] = source_language
        return [TranscriptSegment(id=0, tc_start=1.0, tc_end=2.0, ko=segments[0].ko)]

    monkeypatch.setattr("ingest.asr.align_with_whisperx", fake_align)
    aligned, updated = apply_alignment([segment], quality, "whisperx", "strict", audio_path=tmp_path / "a.mp3", source_language="vi")
    assert seen["source_language"] == "vi"
    assert aligned[0].tc_start == 1.0
    assert updated.timecode_quality == "strict"


def test_apply_alignment_passes_japanese_language_to_whisperx(monkeypatch, tmp_path: Path) -> None:
    segment = TranscriptSegment(id=0, tc_start=0, tc_end=20, ko="こんにちは")
    quality = TranscriptQuality(asr_provider="openai-gpt4o-hybrid", timecode_quality="approximate", approximate_timecodes=True)
    seen = {}

    def fake_align(segments, audio_path, device, source_language="ko"):  # type: ignore[no-untyped-def]
        seen["source_language"] = source_language
        return [TranscriptSegment(id=0, tc_start=1.0, tc_end=2.0, ko=segments[0].ko)]

    monkeypatch.setattr("ingest.asr.align_with_whisperx", fake_align)
    aligned, updated = apply_alignment([segment], quality, "whisperx", "strict", audio_path=tmp_path / "a.mp3", source_language="ja")
    assert seen["source_language"] == "ja"
    assert aligned[0].tc_start == 1.0
    assert updated.timecode_quality == "strict"

def test_whisperx_language_code_maps_supported_languages() -> None:
    assert whisperx_language_code("ko") == "ko"
    assert whisperx_language_code("vi") == "vi"
    assert whisperx_language_code("ja") == "ja"
