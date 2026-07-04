from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from common.schema import ReviewBeat
from review.models import NarrationBeat

DEFAULT_STYLE_PRESET = "viral-recap-vi"
DEFAULT_STYLE_STRENGTH = "strong"
DEFAULT_TARGET_SENTENCE_CHARS = 160
DEFAULT_MAX_SENTENCE_CHARS = 220
DEFAULT_STYLE_SAMPLE = Path("examples/style/viral_recap_vi.cleaned.txt")
SENTENCE_END_RE = re.compile(r"[.!?…]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
PUNCT_RE = re.compile(r"[,.!?;:…]")


@dataclass(frozen=True)
class StyleConfig:
    preset: str = DEFAULT_STYLE_PRESET
    strength: str = DEFAULT_STYLE_STRENGTH
    sample_path: Path | None = DEFAULT_STYLE_SAMPLE
    style_qa: bool = True
    target_sentence_chars: int = DEFAULT_TARGET_SENTENCE_CHARS
    max_sentence_chars: int = DEFAULT_MAX_SENTENCE_CHARS


@dataclass(frozen=True)
class StyleIssue:
    beat_id: int
    type: str
    suggestion: str
    sentence_length: int | None = None


@dataclass
class StyleQaResult:
    passed: bool
    issues: list[StyleIssue] = field(default_factory=list)
    notes: str = ""

    def model_dump_public(self) -> dict:
        return {
            "pass": self.passed,
            "issues": [issue.__dict__ for issue in self.issues],
            "notes": self.notes,
        }


def read_clean_style_sample(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def style_config_key(config: StyleConfig, sample: str) -> dict:
    return {
        "style_preset": config.preset,
        "style_strength": config.strength,
        "style_sample_path": str(config.sample_path) if config.sample_path else None,
        "style_sample_hash": hashlib.sha256(sample.encode("utf-8")).hexdigest() if sample else None,
        "style_qa": config.style_qa,
        "target_sentence_chars": config.target_sentence_chars,
        "max_sentence_chars": config.max_sentence_chars,
    }


def build_style_guide(config: StyleConfig, sample: str = "") -> str:
    strength_line = "Use the style strongly, but never copy wording from the sample." if config.strength == "strong" else "Use the style moderately."
    sample_block = f"\nCLEANED STYLE SAMPLE (punctuation is intentional; imitate rhythm, not exact words):\n{sample[:1800]}\n" if sample else ""
    return f"""
STYLE PRESET: {config.preset}
STYLE STRENGTH: {config.strength}
{strength_line}

STYLE TARGET:
- Vietnamese viral movie recap narration: fast, dramatic, casual, slightly witty.
- Retell and localize the plot; do not translate original dialogue literally.
- Use natural Vietnamese wording and consistent character names/danh xung.
- Light slang or playful commentary is allowed, but avoid crude or confusing language.
- Keep every beat TTS-friendly: clear punctuation, natural pauses, no bullet points, no line breaks inside narration.
- Do NOT imitate raw transcript formatting, missing punctuation, or long run-on sentences.
- Each sentence should usually be 60-{config.target_sentence_chars} Vietnamese characters.
- Avoid sentences over {config.max_sentence_chars} characters; split when action, scene, speaker, or emotion changes.
- Each beat should contain multiple readable sentences when it covers more than one action.
{sample_block}
""".strip()


def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    parts = [part.strip() for part in SENTENCE_SPLIT_RE.split(normalized) if part.strip()]
    return parts or [normalized]


def check_readability(beats: list[ReviewBeat] | list[NarrationBeat], config: StyleConfig) -> StyleQaResult:
    issues: list[StyleIssue] = []
    for beat in beats:
        text = getattr(beat, "narration")
        sentences = split_sentences(text)
        punctuation_count = len(PUNCT_RE.findall(text))
        sentence_end_count = len(SENTENCE_END_RE.findall(text))
        longest = max((len(sentence) for sentence in sentences), default=0)
        if longest > config.max_sentence_chars:
            issues.append(StyleIssue(
                beat_id=beat.beat_id,
                type="sentence_too_long",
                suggestion=f"Split long sentence(s). Keep each sentence under {config.max_sentence_chars} characters with natural punctuation.",
                sentence_length=longest,
            ))
        if len(text) > config.max_sentence_chars and sentence_end_count == 0:
            issues.append(StyleIssue(
                beat_id=beat.beat_id,
                type="missing_sentence_punctuation",
                suggestion="Add sentence-ending punctuation and split the beat into readable TTS-friendly sentences.",
                sentence_length=len(text),
            ))
        if len(text) > config.max_sentence_chars * 1.3 and len(sentences) <= 1:
            issues.append(StyleIssue(
                beat_id=beat.beat_id,
                type="single_run_on_sentence",
                suggestion="This beat is one long run-on sentence. Rewrite as 2-5 concise sentences.",
                sentence_length=len(text),
            ))
        if len(text) > 120 and punctuation_count < 2:
            issues.append(StyleIssue(
                beat_id=beat.beat_id,
                type="too_few_pauses",
                suggestion="Add commas or periods to create clear TTS pauses without slowing the recap style.",
                sentence_length=len(text),
            ))
    return StyleQaResult(passed=not issues, issues=issues, notes="ok" if not issues else "readability issues found")


def issue_to_prompt(issue: StyleIssue) -> str:
    detail = f" ({issue.sentence_length} chars)" if issue.sentence_length else ""
    return f"{issue.type}{detail}: {issue.suggestion}"
