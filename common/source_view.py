"""FILM_MAP lines shown to an LLM as the source of a beat or sentence (G2 review, series composer)."""

from __future__ import annotations

from collections.abc import Sequence

from common.schema import FilmMapSegment

# Per-segment cap keeps source-grounded prompts well under gateway limits.
SOURCE_SEGMENT_CHARS = 220


def segment_text(segment: FilmMapSegment) -> str:
    text = (segment.en if segment.type == "speech" else segment.scene_desc) or ""
    return " ".join(text.split())[:SOURCE_SEGMENT_CHARS]


def build_source_view(film_map: Sequence[FilmMapSegment], spans: Sequence[tuple[int, int]], *, context: int = 0) -> str:
    """FILM_MAP lines for the given segment spans; with context, lines outside every span are tagged [other beat]."""
    lines: list[str] = []
    for segment in film_map:
        if not any(start - context <= segment.id <= end + context for start, end in spans):
            continue
        tag = "" if any(start <= segment.id <= end for start, end in spans) else " [other beat]"
        lines.append(f"#{segment.id}{tag} {segment_text(segment)}")
    return "\n".join(lines)
