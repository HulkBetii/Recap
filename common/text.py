from __future__ import annotations

import re

# Sentence ends at ., !, ? or an ellipsis followed by whitespace. G2 (sentence refs), G3 (sentence timing)
# and G5 (sentence fill) must split narration identically, so this is the only splitter they use.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u2026])\s+")


def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(normalized) if part.strip()]
