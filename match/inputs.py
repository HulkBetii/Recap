from __future__ import annotations

import json
from pathlib import Path

from common.inputs import load_shots as common_load_shots
from common.schema import BeatTiming, FilmMapSegment, ReviewBeat, Shot, validate_beats_timing, validate_film_map


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
    return common_load_shots(path)


def load_film_map(path: Path) -> list[FilmMapSegment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [FilmMapSegment.model_validate(item) for item in data]
    return validate_film_map(segments)
