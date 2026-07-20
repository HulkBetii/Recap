from __future__ import annotations

import json
from pathlib import Path

from common.schema import Shot, validate_shots


def load_shots(path: Path) -> list[Shot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = [Shot.model_validate(item) for item in data]
    return validate_shots(shots)
