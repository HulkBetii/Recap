from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

SuggestBackend = Literal["off", "chatgpt_playwright", "openai_api"]
ALL_CAPS_RE = re.compile(r"\b[A-Z\u0110]{2,12}\b", re.UNICODE)
CAMEL_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]*\b")
DIGIT_SYMBOL_RE = re.compile(r"\b\d+[\w%/.-]*\b", re.UNICODE)
SLASH_RE = re.compile(r"\S+/\S+")

KNOWN_SAFE = {"AI", "API", "TTS", "GPT", "CEO", "FBI", "DNA", "USB", "VIP", "URL"}

@dataclass
class PronunciationRisk:
    beat_id: int
    token: str
    risk_type: str
    reason: str
    suggestion: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "token": self.token,
            "risk_type": self.risk_type,
            "reason": self.reason,
            "suggestion": self.suggestion,
        }

@dataclass
class PronunciationQaReport:
    enabled: bool
    suggest_backend: SuggestBackend
    n_risks: int
    risks: list[PronunciationRisk] = field(default_factory=list)
    lexicon_candidates: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "suggest_backend": self.suggest_backend,
            "n_risks": self.n_risks,
            "risks": [risk.to_json() for risk in self.risks],
            "lexicon_candidates": self.lexicon_candidates,
            "warnings": self.warnings,
        }

def analyze_pronunciation_risks(items: list[Any], *, enabled: bool = True, suggest_backend: SuggestBackend = "off") -> PronunciationQaReport:
    if suggest_backend not in {"off", "chatgpt_playwright", "openai_api"}:
        raise ValueError("pronunciation suggest backend must be off|chatgpt_playwright|openai_api")
    if not enabled:
        return PronunciationQaReport(enabled=False, suggest_backend=suggest_backend, n_risks=0)
    risks: list[PronunciationRisk] = []
    seen: set[tuple[int, str, str]] = set()
    for item in items:
        beat_id = int(item.beat_id)
        text = f"{item.original_text} {item.tts_text}"
        for match in ALL_CAPS_RE.finditer(text):
            token = match.group(0)
            if token in KNOWN_SAFE:
                continue
            add_risk(risks, seen, beat_id, token, "unknown_acronym", "uppercase token may be read incorrectly")
        for match in CAMEL_RE.finditer(text):
            add_risk(risks, seen, beat_id, match.group(0), "camel_case", "mixed-case token may need custom pronunciation")
        for match in SLASH_RE.finditer(text):
            token = match.group(0)
            if token == "24/7":
                continue
            add_risk(risks, seen, beat_id, token, "slash_token", "slash token may need spoken wording")
        for match in DIGIT_SYMBOL_RE.finditer(text):
            token = match.group(0)
            if token.isdigit():
                continue
            add_risk(risks, seen, beat_id, token, "number_symbol", "number/unit/symbol token may need normalization")
    warnings: list[str] = []
    lexicon_candidates: dict[str, str] = {}
    if suggest_backend != "off" and risks:
        warnings.append(f"{suggest_backend} suggestion backend is configured but not invoked automatically in deterministic QA; review risks and add lexicon entries before TTS rerun")
        for risk in risks:
            lexicon_candidates.setdefault(risk.token, "")
    return PronunciationQaReport(
        enabled=True,
        suggest_backend=suggest_backend,
        n_risks=len(risks),
        risks=risks,
        lexicon_candidates=lexicon_candidates,
        warnings=warnings,
    )

def add_risk(risks: list[PronunciationRisk], seen: set[tuple[int, str, str]], beat_id: int, token: str, risk_type: str, reason: str) -> None:
    key = (beat_id, token, risk_type)
    if key in seen:
        return
    seen.add(key)
    risks.append(PronunciationRisk(beat_id=beat_id, token=token, risk_type=risk_type, reason=reason))
