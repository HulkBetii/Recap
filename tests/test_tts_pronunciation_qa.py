from __future__ import annotations

from tts.pronunciation_qa import analyze_pronunciation_risks
from tts.sanitize import normalize_tts_text


def test_pronunciation_qa_flags_unknown_acronym_without_llm() -> None:
    items = [normalize_tts_text("??i SWAT xu?t hi?n.", mode="vi", beat_id=0)]
    report = analyze_pronunciation_risks(items, enabled=True, suggest_backend="off")

    assert report.n_risks == 1
    assert report.risks[0].token == "SWAT"
    assert report.lexicon_candidates == {}


def test_pronunciation_qa_backend_only_suggests_candidates() -> None:
    items = [normalize_tts_text("??i SWAT xu?t hi?n.", mode="vi", beat_id=0)]
    report = analyze_pronunciation_risks(items, enabled=True, suggest_backend="chatgpt_playwright")

    assert report.n_risks == 1
    assert report.lexicon_candidates == {"SWAT": ""}
    assert any("not invoked automatically" in warning for warning in report.warnings)


def test_pronunciation_qa_can_be_disabled() -> None:
    items = [normalize_tts_text("??i SWAT xu?t hi?n.", mode="vi", beat_id=0)]
    report = analyze_pronunciation_risks(items, enabled=False, suggest_backend="off")

    assert report.enabled is False
    assert report.n_risks == 0
