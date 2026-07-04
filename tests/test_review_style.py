from __future__ import annotations

from pathlib import Path

from common.schema import ReviewBeat
from review.style import StyleConfig, build_style_guide, check_readability, read_clean_style_sample


def beat(text: str) -> ReviewBeat:
    return ReviewBeat(beat_id=0, narration=text, from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=1, is_hook=True)


def test_style_guide_contains_tts_and_punctuation_rules() -> None:
    guide = build_style_guide(StyleConfig(max_sentence_chars=220, target_sentence_chars=160), "Mẫu sạch có dấu câu.")
    assert "Vietnamese viral movie recap" in guide
    assert "Do NOT imitate raw transcript formatting" in guide
    assert "Avoid sentences over 220" in guide
    assert "TTS-friendly" in guide


def test_readability_rejects_long_unpunctuated_sentence() -> None:
    text = "đây là một câu rất dài " * 25
    result = check_readability([beat(text)], StyleConfig(max_sentence_chars=120, target_sentence_chars=80))
    assert not result.passed
    assert {issue.type for issue in result.issues} >= {"sentence_too_long", "missing_sentence_punctuation"}


def test_readability_passes_tts_friendly_text() -> None:
    text = "Mở đầu phim là một cuộc rượt đuổi căng thẳng. Nam chính tưởng mình đã thoát, nhưng biến cố mới thật sự bắt đầu."
    result = check_readability([beat(text)], StyleConfig(max_sentence_chars=160, target_sentence_chars=100))
    assert result.passed


def test_read_clean_style_sample_reads_utf8(tmp_path: Path) -> None:
    path = tmp_path / "style.txt"
    path.write_text("Mở đầu phim là một cảnh rất căng.", encoding="utf-8")
    assert "Mở đầu" in read_clean_style_sample(path)
