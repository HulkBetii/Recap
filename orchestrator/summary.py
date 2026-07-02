from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass
class StageSummary:
    stage: str
    status: str
    duration_s: float = 0.0
    command: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    error: str | None = None


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect_warnings(paths: list[Path]) -> list[str]:
    warnings: list[str] = []
    for path in paths:
        payload = read_json(path)
        if isinstance(payload, dict):
            for warning in payload.get("warnings", []) or []:
                warnings.append(f"{path.name}: {warning}")
            if "warnings_count" in payload and payload["warnings_count"]:
                warnings.append(f"{path.name}: warnings_count={payload['warnings_count']}")
    return warnings


def write_summary(
    *,
    path: Path,
    stages: list[StageSummary],
    meta_paths: dict[str, Path],
    dry_run: bool = False,
) -> dict[str, Any]:
    tts_meta = read_json(meta_paths["tts"])
    edl_meta = read_json(meta_paths["match"])
    render_meta = read_json(meta_paths["render"])
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "stages": [stage.__dict__ for stage in stages],
        "calibrate": {
            "real_ratio": tts_meta.get("real_ratio") if isinstance(tts_meta, dict) else None,
            "n_beats_widened": edl_meta.get("n_beats_widened") if isinstance(edl_meta, dict) else None,
            "duration_match": render_meta.get("duration_match") if isinstance(render_meta, dict) else None,
        },
        "warnings": collect_warnings(list(meta_paths.values())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
