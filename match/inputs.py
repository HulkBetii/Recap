from __future__ import annotations

import json
from pathlib import Path

from common.schema import BeatTiming, FilmMapSegment, ReviewBeat, ReviewMicroBeat, Shot, validate_beats_timing, validate_film_map, validate_shots


def load_review_script(path: Path) -> list[ReviewBeat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted([ReviewBeat.model_validate(item) for item in data], key=lambda item: item.beat_id)


def load_beats_timing(path: Path) -> list[BeatTiming]:
    data = json.loads(path.read_text(encoding="utf-8"))
    timings = [BeatTiming.model_validate(item) for item in data]
    return validate_beats_timing(timings, pause_s=load_tts_pause(path))

def load_tts_pause(path: Path) -> float:
    meta_path = path.with_name("tts_meta.json")
    if not meta_path.is_file():
        return infer_pause([BeatTiming.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))])
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        return max(0.0, float(payload.get("inter_beat_pause_s", 0.0)))
    except (json.JSONDecodeError, TypeError, ValueError):
        return infer_pause([BeatTiming.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))])

def infer_pause(timings: list[BeatTiming]) -> float:
    ordered = sorted(timings, key=lambda item: item.beat_id)
    gaps = [round(ordered[index].tl_start - ordered[index - 1].tl_end, 3) for index in range(1, len(ordered))]
    positive = [gap for gap in gaps if gap > 1e-3]
    return positive[0] if positive else 0.0


def load_shots(path: Path) -> list[Shot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = [Shot.model_validate(item) for item in data]
    return validate_shots(shots)


def load_film_map(path: Path) -> list[FilmMapSegment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [FilmMapSegment.model_validate(item) for item in data]
    return validate_film_map(segments)


def load_review_micro(path: Path) -> tuple[list[ReviewBeat], list[BeatTiming], dict[int, ReviewMicroBeat]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    micro_items = sorted([ReviewMicroBeat.model_validate(item) for item in data], key=lambda item: item.beat_id)
    beats = [
        ReviewBeat(
            beat_id=item.beat_id,
            narration=item.narration,
            from_seg_id=item.from_seg_id,
            to_seg_id=item.to_seg_id,
            src_tc_start=item.src_tc_start,
            src_tc_end=item.src_tc_end,
            is_hook=item.is_hook,
        )
        for item in micro_items
    ]
    timings = [
        BeatTiming(
            beat_id=item.beat_id,
            audio_path=f"micro/{item.parent_beat_id}-{item.sub_beat_id}.mp3",
            tl_start=item.tl_start,
            tl_end=item.tl_end,
            duration=item.duration,
        )
        for item in micro_items
    ]
    return beats, sorted(timings, key=lambda item: item.beat_id), {item.beat_id: item for item in micro_items}
