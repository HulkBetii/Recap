from __future__ import annotations

import re
import unicodedata

WHITESPACE_RE = re.compile(r"\s+")
ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff"}


def normalize_japanese_tts_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    normalized = "".join(
        char
        for char in normalized
        if char not in ZERO_WIDTH and not (unicodedata.category(char).startswith("C") and char not in "\n\t")
    )
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        raise ValueError("Japanese TTS text became empty after normalization")
    return normalized
