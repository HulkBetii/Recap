from __future__ import annotations

import re
from dataclasses import dataclass

from common.schema import FilmMapSegment, NonStoryRange, StorySection, VideoProfile

SECTION_ORDER = ["setup", "inciting_incident", "conflict", "investigation", "reveal", "climax", "ending"]
CHARACTER_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


@dataclass(frozen=True)
class StoryMapReport:
    warnings: list[str]
    qa: dict


def segment_text(segment: FilmMapSegment) -> str:
    return " ".join(part for part in (segment.en, segment.ko, segment.scene_desc) if part).strip()


def overlaps_non_story(start: float, end: float, ranges: list[NonStoryRange]) -> NonStoryRange | None:
    for item in ranges:
        if start < item.end_s and end > item.start_s:
            return item
    return None


def summarize_segments(segments: list[FilmMapSegment], fallback: str) -> str:
    texts = [segment_text(segment) for segment in segments if segment_text(segment)]
    if not texts:
        return fallback
    joined = " ".join(texts)
    return joined[:260].strip()


def extract_characters(segments: list[FilmMapSegment]) -> list[str]:
    names: dict[str, int] = {}
    for segment in segments:
        for match in CHARACTER_RE.findall(segment.en or ""):
            if match.lower() in {"The", "This", "That", "When", "After", "Before", "They", "There"}:
                continue
            names[match] = names.get(match, 0) + 1
    return [name for name, _ in sorted(names.items(), key=lambda item: (-item[1], item[0]))[:6]]


def build_story_sections(
    film_map: list[FilmMapSegment],
    *,
    duration_s: float,
    video_profile: VideoProfile | None,
    content_type: str = "movie",
    target_story_sections: int = 7,
) -> tuple[list[StorySection], StoryMapReport]:
    warnings: list[str] = []
    if content_type != "movie":
        warnings.append("storymap v1 is optimized for movie; episode uses coarse sections")
    non_story_ranges = video_profile.non_story_ranges if video_profile else []
    story_segments = [segment for segment in film_map if not overlaps_non_story(segment.tc_start, segment.tc_end, non_story_ranges)]
    non_story_sections: list[StorySection] = []
    for item in non_story_ranges:
        overlapping_ids = [segment.id for segment in film_map if segment.tc_start < item.end_s and segment.tc_end > item.start_s]
        non_story_sections.append(
            StorySection(
                section_id=0,
                type="non_story",
                tc_start=round(item.start_s, 3),
                tc_end=round(item.end_s, 3),
                segment_ids=overlapping_ids,
                summary=f"Non-story range: {item.label}",
                characters=[],
                locations=[],
                events=[item.label],
                confidence=item.confidence,
                warnings=[],
            )
        )
    if not story_segments:
        warnings.append("no story segments after applying video_profile")
        sections = non_story_sections
        return reassign_sections(sections), StoryMapReport(warnings=warnings, qa=build_qa(reassign_sections(sections), warnings))

    n_sections = max(1, min(target_story_sections, len(story_segments)))
    sections: list[StorySection] = []
    for index in range(n_sections):
        start_index = round(len(story_segments) * index / n_sections)
        end_index = round(len(story_segments) * (index + 1) / n_sections)
        bucket = story_segments[start_index:end_index]
        if not bucket:
            continue
        section_type = SECTION_ORDER[min(index, len(SECTION_ORDER) - 1)]
        tc_start = max(0.0, min(bucket[0].tc_start, duration_s))
        tc_end = max(0.0, min(bucket[-1].tc_end, duration_s))
        if tc_end <= tc_start:
            continue
        summary = summarize_segments(bucket, f"Movie {section_type.replace('_', ' ')} section")
        characters = extract_characters(bucket)
        section_warnings: list[str] = []
        if tc_end - tc_start > 900:
            section_warnings.append("section too long")
        if not characters and section_type in {"setup", "inciting_incident", "conflict"}:
            section_warnings.append("no obvious character names")
        rounded_start = round(tc_start, 3)
        rounded_end = min(duration_s, round(tc_end, 3))
        if rounded_end <= rounded_start:
            continue
        sections.append(
            StorySection(
                section_id=0,
                type=section_type,  # reassigned below
                tc_start=rounded_start,
                tc_end=rounded_end,
                segment_ids=[segment.id for segment in bucket],
                summary=summary,
                characters=characters,
                locations=[],
                events=[summary[:120]],
                confidence=0.75 if section_warnings else 0.82,
                warnings=section_warnings,
            )
        )
    sections.extend(non_story_sections)
    ordered = reassign_sections(sorted(sections, key=lambda item: (item.tc_start, item.tc_end, item.type == "non_story")))
    return ordered, StoryMapReport(warnings=warnings, qa=build_qa(ordered, warnings))


def reassign_sections(sections: list[StorySection]) -> list[StorySection]:
    return [section.model_copy(update={"section_id": index}) for index, section in enumerate(sections)]


def build_qa(sections: list[StorySection], warnings: list[str]) -> dict:
    section_warnings = [
        {"section_id": section.section_id, "warnings": section.warnings}
        for section in sections
        if section.warnings
    ]
    return {
        "n_sections": len(sections),
        "n_non_story": sum(1 for section in sections if section.type == "non_story"),
        "warnings": warnings,
        "section_warnings": section_warnings,
    }
