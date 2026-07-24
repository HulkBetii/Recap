from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any, Sequence

BLOCKING_NARRATION_QA_CODES = {
    "foreign_language_in_narration",
    "unaccented_vietnamese_narration",
    "intra_beat_sentence_repetition",
    "repeated_narration_template",
    "generic_fallback_narration",
    "cross_beat_similarity_too_high",
}

VIETNAMESE_DIACRITIC_RE = re.compile(r"[\u00c0-\u024f\u1ea0-\u1eff]")
FOREIGN_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
FOREIGN_PHRASE_RE = re.compile(
    r"\b("
    r"since|seems|release|sorry|summoning|wake up|commander|"
    r"they(?:'re| are)?|battle|extract|system|quest|hunter|"
    r"probably|thought|target|option|orchestrated|"
    r"artillery|knight|shadow|solo leveling"
    r")\b",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")

GENERIC_FALLBACK_PHRASES = (
    "chuyen can giu lai la",
    "day la mot buoc ngoat cua mach truyen",
    "mach truyen giu moc quan trong nay",
    "moc nay quan trong",
    "lam thay doi trang thai cau chuyen",
    "giu cho ban recap co du ngu canh",
    "de khong bi qua voi",
    "mat xich nhan qua",
    "khong phai phan lap lai doi thoai",
    "ke lai tung cau nguyen van",
)

def contains_vietnamese_diacritics(text: str) -> bool:
    return bool(VIETNAMESE_DIACRITIC_RE.search(text))

def strip_diacritics(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.replace("đ", "d").replace("Đ", "D")

def contains_foreign_text(text: str) -> bool:
    if FOREIGN_SCRIPT_RE.search(text):
        return True
    return bool(FOREIGN_PHRASE_RE.search(strip_diacritics(text).lower()))

def normalize_for_similarity(text: str) -> str:
    normalized = strip_diacritics(text).lower()
    normalized = FOREIGN_SCRIPT_RE.sub(" ", normalized)
    normalized = FOREIGN_PHRASE_RE.sub(" ", normalized)
    normalized = re.sub(r"\bs\d{1,2}e\d{1,3}\b", " episode ", normalized)
    normalized = re.sub(r"\b(?:tap|episode|ep)\s*\d+\b", " episode ", normalized)
    normalized = re.sub(r"\d+", " <num> ", normalized)
    normalized = re.sub(r"[^a-z0-9<>\s]+", " ", normalized)
    return WHITESPACE_RE.sub(" ", normalized).strip()

def clean_summary_for_tts(text: str) -> str:
    cleaned = WHITESPACE_RE.sub(" ", text).strip()
    if not cleaned:
        return ""
    cleaned = FOREIGN_SCRIPT_RE.sub(" ", cleaned)
    cleaned = FOREIGN_PHRASE_RE.sub(" ", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()
    # Keep the original accented text when possible, but drop whole foreign-heavy
    # sentences below via duplicate/safety checks.
    cleaned = ". ".join(sentence for sentence in split_sentences(cleaned) if contains_vietnamese_diacritics(sentence))
    cleaned = collapse_duplicate_sentences(cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()
    if not cleaned or not contains_vietnamese_diacritics(cleaned):
        return ""
    return cleaned

def split_sentences(text: str) -> list[str]:
    normalized = WHITESPACE_RE.sub(" ", text).strip()
    if not normalized:
        return []
    return [part.strip(" ,.;:!?") for part in SENTENCE_SPLIT_RE.split(normalized) if part.strip(" ,.;:!?")]

def collapse_duplicate_sentences(text: str) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return WHITESPACE_RE.sub(" ", text).strip()
    seen: set[str] = set()
    output: list[str] = []
    for sentence in sentences:
        signature = normalize_for_similarity(sentence)
        if len(signature) >= 24:
            if signature in seen:
                continue
            seen.add(signature)
        output.append(sentence)
    return ". ".join(output)

def safe_summary_fragment(summary: str, limit: int = 240) -> str:
    cleaned = clean_summary_for_tts(summary)
    if not cleaned or contains_foreign_text(cleaned):
        return ""
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "..."

def _item_field(item: Any, field: str, default: Any) -> Any:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)

def _record_items(items: Sequence[Any]) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        raw_id = _item_field(item, "beat_id", index)
        try:
            beat_id = int(raw_id)
        except (TypeError, ValueError):
            beat_id = index
        narration = str(_item_field(item, "narration", ""))
        records.append((beat_id, narration))
    return records

def _template_signature(text: str, *, token_count: int = 16) -> str:
    tokens = normalize_for_similarity(text).split()
    if len(tokens) < 6:
        return ""
    return " ".join(tokens[:token_count])

def _repeated_sentence_issue(beat_id: int, text: str) -> dict[str, object] | None:
    signatures = [
        normalize_for_similarity(sentence)
        for sentence in split_sentences(text)
    ]
    counts = Counter(signature for signature in signatures if len(signature) >= 32)
    repeated = [signature for signature, count in counts.items() if count >= 2]
    if not repeated:
        return None
    return {
        "level": "error",
        "code": "intra_beat_sentence_repetition",
        "message": "Narration repeats the same sentence inside one beat",
        "beat_ids": [beat_id],
        "sentence_signature": repeated[0][:120],
    }

def _substantive_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", strip_diacritics(text), flags=re.UNICODE))

def analyze_narration_content(
    items: Sequence[Any],
    *,
    similarity_threshold: float = 0.92,
) -> list[dict[str, object]]:
    records = _record_items(items)
    if not records:
        return []

    issues: list[dict[str, object]] = []
    foreign_beats: list[int] = []
    unaccented_beats: list[int] = []
    template_hits: dict[str, list[int]] = defaultdict(list)
    generic_phrase_hits: dict[str, list[int]] = defaultdict(list)
    normalized_texts: list[tuple[int, str]] = []

    for beat_id, narration in records:
        if contains_foreign_text(narration):
            foreign_beats.append(beat_id)
        elif (
            LATIN_LETTER_RE.search(narration)
            and _substantive_word_count(narration) >= 4
            and not contains_vietnamese_diacritics(narration)
        ):
            unaccented_beats.append(beat_id)

        sentence_issue = _repeated_sentence_issue(beat_id, narration)
        if sentence_issue is not None:
            issues.append(sentence_issue)

        normalized = normalize_for_similarity(narration)
        normalized_texts.append((beat_id, normalized))
        signature = _template_signature(narration)
        if signature:
            template_hits[signature].append(beat_id)
        for phrase in GENERIC_FALLBACK_PHRASES:
            if phrase in normalized:
                generic_phrase_hits[phrase].append(beat_id)

    if foreign_beats:
        issues.append(
            {
                "level": "error",
                "code": "foreign_language_in_narration",
                "message": "Narration contains untranslated foreign script or phrases",
                "beat_ids": foreign_beats,
            }
        )
    if unaccented_beats:
        issues.append(
            {
                "level": "error",
                "code": "unaccented_vietnamese_narration",
                "message": "Narration does not contain Vietnamese diacritics",
                "beat_ids": unaccented_beats,
            }
        )

    repeat_threshold = max(4, min(8, math.ceil(len(records) * 0.2)))
    repeated_templates = [
        (signature, beat_ids)
        for signature, beat_ids in template_hits.items()
        if len(beat_ids) >= repeat_threshold
    ]
    if repeated_templates:
        beat_ids = sorted({beat_id for _signature, ids in repeated_templates for beat_id in ids})
        issues.append(
            {
                "level": "error",
                "code": "repeated_narration_template",
                "message": "Too many beats reuse the same narration template",
                "beat_ids": beat_ids[:40],
                "template_count": len(repeated_templates),
                "template_sample": repeated_templates[0][0][:160],
            }
        )

    generic_threshold = max(3, min(5, math.ceil(len(records) * 0.12)))
    generic_repeats = [
        (phrase, sorted(set(beat_ids)))
        for phrase, beat_ids in generic_phrase_hits.items()
        if len(set(beat_ids)) >= generic_threshold
    ]
    if generic_repeats:
        beat_ids = sorted({beat_id for _phrase, ids in generic_repeats for beat_id in ids})
        issues.append(
            {
                "level": "error",
                "code": "generic_fallback_narration",
                "message": "Narration contains repeated generic fallback phrases",
                "beat_ids": beat_ids[:40],
                "phrases": [phrase for phrase, _ids in generic_repeats[:8]],
            }
        )

    similar_pairs: list[tuple[int, int, float]] = []
    for left_index, (left_id, left_text) in enumerate(normalized_texts):
        if len(left_text) < 48:
            continue
        for right_id, right_text in normalized_texts[left_index + 1 :]:
            if len(right_text) < 48:
                continue
            max_len = max(len(left_text), len(right_text))
            if abs(len(left_text) - len(right_text)) / max_len > 0.35:
                continue
            ratio = SequenceMatcher(None, left_text, right_text).ratio()
            if ratio >= similarity_threshold:
                similar_pairs.append((left_id, right_id, round(ratio, 4)))

    similarity_threshold_count = max(4, len(records) // 8)
    if len(similar_pairs) >= similarity_threshold_count:
        beat_ids = sorted({beat_id for left_id, right_id, _ratio in similar_pairs for beat_id in (left_id, right_id)})
        if len(beat_ids) >= 4:
            issues.append(
                {
                    "level": "error",
                    "code": "cross_beat_similarity_too_high",
                    "message": "Too many beats are textually similar after removing episode numbers",
                    "beat_ids": beat_ids[:40],
                    "similar_pair_count": len(similar_pairs),
                    "max_ratio": max(ratio for _left_id, _right_id, ratio in similar_pairs),
                }
            )

    return issues
