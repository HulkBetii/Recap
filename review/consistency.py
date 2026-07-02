from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from review.models import NarrationBeat


@dataclass(frozen=True)
class CanonicalTerm:
    canonical: str
    aliases: tuple[str, ...]


def extract_canonical_terms(glossary: list[dict[str, Any]]) -> list[CanonicalTerm]:
    terms: dict[str, set[str]] = {}
    for entry in glossary:
        for value in iter_entry_terms(entry):
            normalized = normalize_spaces(value)
            if is_useful_term(normalized):
                terms.setdefault(normalized, set()).update(generate_aliases(normalized))
    return [CanonicalTerm(canonical=term, aliases=tuple(sorted(aliases - {term}, key=len, reverse=True))) for term, aliases in terms.items()]


def iter_entry_terms(entry: dict[str, Any]):  # type: ignore[no-untyped-def]
    for key in ("canonical", "canonical_name", "name", "vi", "term"):
        value = entry.get(key)
        if isinstance(value, str):
            yield value
    aliases = entry.get("aliases") or entry.get("alias")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str):
                yield alias
    role = entry.get("role") or entry.get("description")
    if isinstance(role, str):
        yield from extract_latin_name_phrases(role)


def extract_latin_name_phrases(text: str) -> list[str]:
    pattern = re.compile(r"\b(?:[A-Z][a-z]+)(?:[ -][A-Z][a-z]+|[ -][A-Z][a-z]+-[a-z]+){1,3}\b")
    return [match.group(0).strip(" ,.;:") for match in pattern.finditer(text)]


def normalize_spaces(value: str) -> str:
    return " ".join(value.replace("–", "-").replace("—", "-").split())


def is_useful_term(value: str) -> bool:
    if len(value) < 4:
        return False
    if value.lower() in {"nhân vật", "tổng giám", "nữ lãnh"}:
        return False
    return True


def generate_aliases(canonical: str) -> set[str]:
    aliases = {canonical}
    aliases.add(canonical.replace("-", " "))
    aliases.add(canonical.replace(" ", "-"))
    replacements = {
        "Seong": ("Seon", "Sung", "Song"),
        "Jun-hyun": ("Junhyun", "Jun Hyun", "Jun-hyeon", "Junhyeon"),
        "Sang-jae": ("Sangjae", "Sang Jae"),
    }
    for correct, variants in replacements.items():
        if correct in canonical:
            for variant in variants:
                aliases.add(canonical.replace(correct, variant))
    return {normalize_spaces(alias) for alias in aliases if alias.strip()}


def apply_narration_consistency(
    narration: list[NarrationBeat],
    glossary: list[dict[str, Any]],
) -> tuple[list[NarrationBeat], list[str]]:
    terms = extract_canonical_terms(glossary)
    if not terms:
        return narration, []
    changed_beats: list[int] = []
    output: list[NarrationBeat] = []
    for beat in narration:
        text = beat.narration
        corrected = text
        for term in terms:
            for alias in term.aliases:
                if alias == term.canonical:
                    continue
                corrected = replace_alias(corrected, alias, term.canonical)
        if corrected != text:
            changed_beats.append(beat.beat_id)
        output.append(beat.model_copy(update={"narration": corrected}))
    warnings = [f"narration consistency normalized glossary terms in beat(s): {', '.join(map(str, changed_beats))}"] if changed_beats else []
    return output, warnings


def replace_alias(text: str, alias: str, canonical: str) -> str:
    if not alias or alias == canonical:
        return text
    escaped = re.escape(alias)
    pattern = re.compile(rf"(?<![\w-]){escaped}(?![\w-])")
    return pattern.sub(canonical, text)
