from __future__ import annotations

from common.schema import FilmMapSegment


def build_film_map_view(film_map: list[FilmMapSegment]) -> str:
    lines: list[str] = []
    for segment in film_map:
        text = segment.en if segment.type == "speech" else segment.scene_desc
        lines.append(f"#{segment.id} [{segment.tc_start:.3f}-{segment.tc_end:.3f}] {text}")
    return "\n".join(lines)


def read_style_sample(path: str | None) -> str:
    if not path:
        return ""
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8").strip()
