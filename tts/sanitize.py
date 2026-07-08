from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

NormalizationMode = Literal["off", "basic", "vi"]

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b", re.UNICODE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
MARKDOWN_STYLE_RE = re.compile(r'[\*_`~#>"\[\]]+')
SLASH_INSIDE_WORD = re.compile(r"(?<=\w)\s*/\s*(?=\w)", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
ACRONYM_RE = re.compile(r"\b[A-Z\u0110]{2,8}\b", re.UNICODE)

DEFAULT_LEXICON: dict[str, str] = {
    "AI": "\u00e2y ai",
    "A.I.": "\u00e2y ai",
    "ChatGPT": "chat gi pi ti",
    "GPT": "gi pi ti",
    "TTS": "ti ti \u00e9t",
    "API": "\u00e2y pi ai",
    "URL": "u r\u1edd l\u1edd",
    "CEO": "si i \u00f4",
    "FBI": "\u00e9p bi ai",
    "DNA": "\u0111i en \u00e2y",
    "USB": "u \u00e9t bi",
    "VIP": "vi ai pi",
}

ACRONYM_LETTER_READINGS: dict[str, str] = {
    "A": "\u00e2y",
    "B": "bi",
    "C": "si",
    "D": "\u0111i",
    "E": "i",
    "F": "\u00e9p",
    "G": "gi",
    "H": "h\u00e1t",
    "I": "ai",
    "J": "gi\u00e2y",
    "K": "ca",
    "L": "eo",
    "M": "em",
    "N": "en",
    "O": "\u00f4",
    "P": "pi",
    "Q": "quy",
    "R": "a r\u1edd",
    "S": "\u00e9t",
    "T": "ti",
    "U": "u",
    "V": "vi",
    "W": "\u0111\u1eafp liu",
    "X": "\u00edch",
    "Y": "quai",
    "Z": "d\u00e9t",
}

UNIT_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\d+(?:[,.]\d+)?)\s*%"), "\\g<1> ph\u1ea7n tr\u0103m"),
    (re.compile(r"(\d+(?:[,.]\d+)?)\s*km\b", re.IGNORECASE), "\\g<1> ki l\u00f4 m\u00e9t"),
    (re.compile(r"(\d+(?:[,.]\d+)?)\s*kg\b", re.IGNORECASE), "\\g<1> ki l\u00f4 gam"),
    (re.compile(r"(\d+(?:[,.]\d+)?)\s*m2\b", re.IGNORECASE), "\\g<1> m\u00e9t vu\u00f4ng"),
    (re.compile(r"\b24\s*/\s*7\b"), "hai t\u01b0 tr\u00ean b\u1ea3y"),
]

EMOJI_AND_SYMBOL_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\u2600-\u27BF"
    "]+",
    re.UNICODE,
)

@dataclass
class TtsTextItem:
    beat_id: int
    original_text: str
    tts_text: str
    changed: bool
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "original_text": self.original_text,
            "tts_text": self.tts_text,
            "changed": self.changed,
            "rules_applied": self.rules_applied,
            "warnings": self.warnings,
        }

@dataclass
class TtsNormalizationReport:
    mode: NormalizationMode
    pronunciation_lexicon_path: str | None
    n_items: int
    n_changed: int
    warnings: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "pronunciation_lexicon_path": self.pronunciation_lexicon_path,
            "n_items": self.n_items,
            "n_changed": self.n_changed,
            "warnings": self.warnings,
        }

def load_pronunciation_lexicon(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError(f"pronunciation lexicon does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ValueError("PyYAML is required to read YAML pronunciation lexicon") from exc
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        raise ValueError("pronunciation lexicon must be .json, .yaml, or .yml")
    if not isinstance(data, dict):
        raise ValueError("pronunciation lexicon must be an object mapping token to pronunciation")
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("pronunciation lexicon keys and values must be strings")
        normalized_key = key.strip()
        normalized_value = value.strip()
        if normalized_key and normalized_value:
            result[normalized_key] = normalized_value
    return result

def sanitize_tts_text(text: str) -> str:
    return normalize_tts_text(text, mode="basic").tts_text

def normalize_tts_text(
    text: str,
    *,
    mode: NormalizationMode = "vi",
    lexicon: dict[str, str] | None = None,
    beat_id: int = -1,
) -> TtsTextItem:
    original = text
    normalized = text.strip()
    rules: list[str] = []
    warnings: list[str] = []
    if mode == "off":
        normalized = WHITESPACE_RE.sub(" ", normalized).strip()
        return TtsTextItem(beat_id, original, normalized, normalized != original, ["trim_whitespace"] if normalized != original else [], [])

    active_lexicon = dict(DEFAULT_LEXICON if mode == "vi" else {"ChatGPT": "chat G P T", "AI": "A I", "TTS": "T T S"})
    if lexicon:
        active_lexicon.update(lexicon)

    normalized, applied = apply_exact_lexicon(normalized, active_lexicon)
    rules.extend(applied)

    normalized = MARKDOWN_LINK_RE.sub(r"\1", normalized)
    if normalized != text:
        rules.append("strip_markdown_links")
    before = normalized
    normalized = URL_RE.sub(" ", normalized)
    normalized = EMAIL_RE.sub(" ", normalized)
    if normalized != before:
        rules.append("strip_url_email")
        warnings.append("removed url/email from TTS text")

    before = normalized
    normalized = MARKDOWN_STYLE_RE.sub("", normalized)
    normalized = EMOJI_AND_SYMBOL_RE.sub(" ", normalized)
    if normalized != before:
        rules.append("strip_markup_symbols")

    for pattern, replacement in UNIT_REPLACEMENTS:
        before = normalized
        normalized = pattern.sub(replacement, normalized)
        if normalized != before:
            rules.append("normalize_units")

    before = normalized
    normalized = SLASH_INSIDE_WORD.sub(" ho\u1eb7c ", normalized)
    if normalized != before:
        rules.append("normalize_slash")

    if mode == "vi":
        normalized, acronym_rules, acronym_warnings = normalize_unknown_acronyms(normalized, active_lexicon)
        rules.extend(acronym_rules)
        warnings.extend(acronym_warnings)

    before = normalized
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    normalized = re.sub(r"\s+([,.!?;:])", r"\1", normalized)
    if normalized != before:
        rules.append("normalize_whitespace")

    if not normalized:
        raise ValueError("TTS text became empty after normalization")
    if len(normalized) < max(2, len(original.strip()) * 0.2):
        warnings.append("TTS text became much shorter after normalization")

    return TtsTextItem(
        beat_id=beat_id,
        original_text=original,
        tts_text=normalized,
        changed=normalized != original,
        rules_applied=dedupe(rules),
        warnings=dedupe(warnings),
    )

def normalize_tts_script(
    beats: list[Any],
    *,
    mode: NormalizationMode = "vi",
    pronunciation_lexicon_path: Path | None = None,
) -> tuple[list[TtsTextItem], TtsNormalizationReport]:
    lexicon = load_pronunciation_lexicon(pronunciation_lexicon_path)
    items = [
        normalize_tts_text(beat.narration, mode=mode, lexicon=lexicon, beat_id=beat.beat_id)
        for beat in beats
    ]
    warnings: list[str] = []
    for item in items:
        warnings.extend(f"beat {item.beat_id}: {warning}" for warning in item.warnings)
    report = TtsNormalizationReport(
        mode=mode,
        pronunciation_lexicon_path=str(pronunciation_lexicon_path) if pronunciation_lexicon_path else None,
        n_items=len(items),
        n_changed=sum(1 for item in items if item.changed),
        warnings=dedupe(warnings),
    )
    return items, report

def apply_exact_lexicon(text: str, lexicon: dict[str, str]) -> tuple[str, list[str]]:
    result = text
    rules: list[str] = []
    for token in sorted(lexicon, key=len, reverse=True):
        replacement = lexicon[token]
        pattern = re.compile(rf"(?<![\w.]){re.escape(token)}(?![\w.])", re.UNICODE)
        result, count = pattern.subn(replacement, result)
        if count:
            rules.append(f"lexicon:{token}")
    return result, rules

def normalize_unknown_acronyms(text: str, lexicon: dict[str, str]) -> tuple[str, list[str], list[str]]:
    rules: list[str] = []
    warnings: list[str] = []
    lexicon_keys = set(lexicon)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in lexicon_keys:
            return token
        if token in {"VND", "USD"}:
            warnings.append(f"unhandled currency acronym: {token}")
            return token
        if len(token) <= 4:
            spoken = " ".join(ACRONYM_LETTER_READINGS.get(char, char.lower()) for char in token)
            rules.append("spell_acronym")
            return spoken
        warnings.append(f"unhandled uppercase token: {token}")
        return token

    return ACRONYM_RE.sub(replace, text), dedupe(rules), dedupe(warnings)

def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
