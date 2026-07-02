from __future__ import annotations

import re

ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ChatGPT", re.IGNORECASE), "chat G P T"),
    (re.compile(r"AI", re.IGNORECASE), "A I"),
    (re.compile(r"TTS", re.IGNORECASE), "T T S"),
]
SLASH_INSIDE_WORD = re.compile(r"(?<=\w)\s*/\s*(?=\w)", re.UNICODE)


def sanitize_tts_text(text: str) -> str:
    sanitized = text.strip()
    for pattern, replacement in ALIASES:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = SLASH_INSIDE_WORD.sub(" ", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized
