from __future__ import annotations

import re
import unicodedata

JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
LETTER_RE = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
MARKDOWN_RE = re.compile(r"```|[*_`#]|\[[^\]]+\]\([^)]+\)")
STYLE_MARKERS = ("お前ら", "ネキ", "ニキ", "ワイ", "草", "だぜ", "じゃね", "すぎる", "地獄")


def japanese_ratio(text: str) -> float:
    letters = LETTER_RE.findall(text)
    if not letters:
        return 0.0
    return sum(1 for char in letters if JAPANESE_RE.fullmatch(char)) / len(letters)


def validate_japanese_text(text: str, *, char_budget: int) -> list[str]:
    errors: list[str] = []
    if not JAPANESE_RE.search(text):
        errors.append("text has no Japanese script")
    if japanese_ratio(text) < 0.60:
        errors.append("Japanese script ratio is below 0.60")
    if URL_RE.search(text):
        errors.append("URLs are not allowed")
    if MARKDOWN_RE.search(text):
        errors.append("Markdown is not allowed")
    if any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in text):
        errors.append("control characters are not allowed")
    if len(text) > max(char_budget + 4, round(char_budget * 1.20)):
        errors.append(f"text exceeds character budget {char_budget}")
    return errors


def detected_tone_tags(text: str) -> list[str]:
    return [marker for marker in STYLE_MARKERS if marker in text]
