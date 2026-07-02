from __future__ import annotations

import json
from pathlib import Path

from common.schema import BeatTiming, ReviewBeat, Shot, validate_beats_timing, validate_shots


def load_review_script(path: Path) -> list[ReviewBeat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted([ReviewBeat.model_validate(item) for item in data], key=lambda item: item.beat_id)


def load_beats_timing(path: Path) -> list[BeatTiming]:
    data = json.loads(path.read_text(encoding="utf-8"))
    timings = [BeatTiming.model_validate(item) for item in data]
    return validate_beats_timing(timings, pause_s=0.0)


def load_shots(path: Path) -> list[Shot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = [Shot.model_validate(item) for item in data]
    return validate_shots(shots)
