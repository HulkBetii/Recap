from __future__ import annotations

from common.schema import FilmMapSegment, TranslatedSegment, VisionSegment, validate_film_map


def build_film_map(
    speech_segments: list[TranslatedSegment],
    visual_segments: list[VisionSegment],
    duration: float,
) -> list[FilmMapSegment]:
    combined: list[FilmMapSegment] = []
    for segment in speech_segments:
        combined.append(
            FilmMapSegment(
                id=0,
                type="speech",
                tc_start=round(segment.tc_start, 3),
                tc_end=round(segment.tc_end, 3),
                ko=segment.ko,
                en=segment.en,
                scene_desc=None,
            )
        )
    for segment in visual_segments:
        combined.append(
            FilmMapSegment(
                id=0,
                type="visual",
                tc_start=round(segment.tc_start, 3),
                tc_end=round(segment.tc_end, 3),
                ko=None,
                en=None,
                scene_desc=segment.scene_desc,
            )
        )
    ordered = sorted(combined, key=lambda item: (item.tc_start, item.tc_end, item.type))
    normalized: list[FilmMapSegment] = []
    for index, segment in enumerate(ordered):
        tc_start = max(0.0, min(segment.tc_start, duration))
        tc_end = max(0.0, min(segment.tc_end, duration))
        if normalized and tc_start < normalized[-1].tc_end:
            tc_start = normalized[-1].tc_end
        if tc_end <= tc_start:
            continue
        normalized.append(
            FilmMapSegment(
                id=index,
                type=segment.type,
                tc_start=tc_start,
                tc_end=tc_end,
                ko=segment.ko,
                en=segment.en,
                scene_desc=segment.scene_desc,
            )
        )
    for index, segment in enumerate(normalized):
        normalized[index] = segment.model_copy(update={"id": index})
    return validate_film_map(normalized, duration)
