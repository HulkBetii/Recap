from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from review.models import QaIssue

WORD_RE = re.compile(r"\b[\w']+\b", flags=re.UNICODE)
SENTENCE_SPLIT_RE = re.compile(r"[.!?;:]+|\n+")


@dataclass(frozen=True)
class RepetitionFinding:
    beat_id: int
    matched_beat_id: int | None
    overlap: float
    reason: str


def normalize_text(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", text).casefold()
    stripped = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    stripped = stripped.replace("đ", "d").replace("Đ", "d")
    stripped = re.sub(r"[^\w\s]", " ", stripped, flags=re.UNICODE)
    return " ".join(stripped.split())


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(normalize_text(text))


def sentence_segments(text: str) -> list[str]:
    parts = [segment.strip() for segment in SENTENCE_SPLIT_RE.split(text) if segment and segment.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def jaccard(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    left = set(a)
    right = set(b)
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def detect_repetition_findings(
    texts: list[tuple[int, str]],
    *,
    lookback: int = 6,
    jaccard_threshold: float = 0.82,
    min_tokens: int = 6,
) -> list[RepetitionFinding]:
    findings: list[RepetitionFinding] = []
    previous_by_norm: dict[str, int] = {}
    previous_tokens: list[tuple[int, list[str]]] = []

    for beat_id, narration in texts:
        normalized = normalize_text(narration)
        tokens = tokenize(narration)
        if len(tokens) < min_tokens:
            previous_by_norm[normalized] = beat_id
            previous_tokens.append((beat_id, tokens))
            continue

        exact_match = previous_by_norm.get(normalized)
        if exact_match is not None and exact_match != beat_id:
            findings.append(
                RepetitionFinding(
                    beat_id=beat_id,
                    matched_beat_id=exact_match,
                    overlap=1.0,
                    reason="exact narration repeat",
                )
            )
            previous_by_norm[normalized] = beat_id
            previous_tokens.append((beat_id, tokens))
            continue

        best_match: tuple[int, float] | None = None
        for other_beat_id, other_tokens in previous_tokens[-lookback:]:
            overlap = jaccard(tokens, other_tokens)
            if overlap >= jaccard_threshold and (best_match is None or overlap > best_match[1]):
                best_match = (other_beat_id, overlap)

        if best_match is not None:
            findings.append(
                RepetitionFinding(
                    beat_id=beat_id,
                    matched_beat_id=best_match[0],
                    overlap=round(best_match[1], 3),
                    reason="near-duplicate narration",
                )
            )

        previous_by_norm[normalized] = beat_id
        previous_tokens.append((beat_id, tokens))

    return findings


def repetition_issues(beats: list[tuple[int, str]]) -> list[QaIssue]:
    findings = detect_repetition_findings(beats)
    issues = [
        QaIssue(
            beat_id=finding.beat_id,
            type="repetition",
            suggestion=(
                f"Giảm lặp ý với beat {finding.matched_beat_id}; "
                f"overlap={finding.overlap:.3f}. Giữ lại ý mới, đổi nhịp và tăng chi tiết hình ảnh."
            ),
        )
        for finding in findings
    ]
    unique: dict[int, QaIssue] = {}
    for issue in issues:
        unique.setdefault(issue.beat_id, issue)
    return [unique[beat_id] for beat_id in sorted(unique)]
