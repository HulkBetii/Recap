from __future__ import annotations

import json

from tts.cache import build_cache_key
from tts.sanitize import load_pronunciation_lexicon, normalize_tts_text


def test_vi_normalizer_keeps_lowercase_ai() -> None:
    item = normalize_tts_text("Không ai biết ai là thủ phạm.", mode="vi")

    assert item.tts_text == "Không ai biết ai là thủ phạm."
    assert "ây ai" not in item.tts_text


def test_vi_normalizer_handles_ai_acronym_only() -> None:
    item = normalize_tts_text("AI và A.I. không giống chữ ai thường.", mode="vi")

    assert item.tts_text == "ây ai và ây ai không giống chữ ai thường."
    assert any(rule.startswith("lexicon:AI") for rule in item.rules_applied)


def test_vi_normalizer_handles_common_acronyms_and_symbols() -> None:
    item = normalize_tts_text("**ChatGPT** gọi API/TTS đạt 90% trong 24/7 😊", mode="vi")

    assert "chat gi pi ti" in item.tts_text
    assert "ây pi ai hoặc ti ti ét" in item.tts_text
    assert "90 phần trăm" in item.tts_text
    assert "hai tư trên bảy" in item.tts_text
    assert "😊" not in item.tts_text


def test_custom_lexicon_overrides_default(tmp_path) -> None:
    path = tmp_path / "lexicon.json"
    path.write_text(json.dumps({"AI": "a i custom"}, ensure_ascii=False), encoding="utf-8")
    lexicon = load_pronunciation_lexicon(path)
    item = normalize_tts_text("AI xuất hiện.", mode="vi", lexicon=lexicon)

    assert item.tts_text == "a i custom xuất hiện."


def test_cache_key_uses_normalized_tts_text() -> None:
    raw = "AI xuất hiện."
    normalized = normalize_tts_text(raw, mode="vi").tts_text

    raw_key = build_cache_key(provider="ai33", voice_id="v", model="m", speed=1.0, narration=raw, normalized=True)
    normalized_key = build_cache_key(provider="ai33", voice_id="v", model="m", speed=1.0, narration=normalized, normalized=True)

    assert raw_key != normalized_key
