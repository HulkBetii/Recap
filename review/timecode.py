from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat, validate_review_script
from review.models import NarrationBeat, OutlineBeat


def derive_review_beats(
    *,
    outline: list[OutlineBeat],
    narration: list[NarrationBeat],
    film_map: list[FilmMapSegment],
) -> list[ReviewBeat]:
    by_id = {segment.id: segment for segment in film_map}
    narration_by_id = {item.beat_id: item.narration for item in narration}
    beats: list[ReviewBeat] = []
    for beat_id, outline_beat in enumerate(outline):
        if outline_beat.from_seg_id not in by_id or outline_beat.to_seg_id not in by_id:
            continue
        if beat_id not in narration_by_id:
            continue
        start_segment = by_id[outline_beat.from_seg_id]
        end_segment = by_id[outline_beat.to_seg_id]
        beats.append(
            ReviewBeat(
                beat_id=len(beats),
                narration=narration_by_id[beat_id],
                from_seg_id=outline_beat.from_seg_id,
                to_seg_id=outline_beat.to_seg_id,
                src_tc_start=start_segment.tc_start,
                src_tc_end=end_segment.tc_end,
                is_hook=outline_beat.is_hook,
            )
        )
    return validate_review_script(beats, film_map)
