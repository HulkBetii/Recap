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
            if path.name == "film_map.meta.json" and payload.get("approximate_timecodes"):
                warnings.append(
                    f"{path.name}: approximate_timecodes=true; footage matching may be less precise"
                )
            for warning in payload.get("warnings", []) or []:
                warnings.append(f"{path.name}: {warning}")
            if "warnings_count" in payload and payload["warnings_count"]:
                warnings.append(f"{path.name}: warnings_count={payload['warnings_count']}")
    return warnings

def build_timecode_qa(film_meta: Any) -> dict[str, Any]:
    if not isinstance(film_meta, dict):
        return {
            "status": "missing",
            "risk_level": "unknown",
            "message": "film_map.meta.json is missing or invalid; timecode quality cannot be assessed",
        }

    approximate = bool(film_meta.get("approximate_timecodes", False))
    quality = film_meta.get("timecode_quality", "strict")
    asr_warnings = list(film_meta.get("asr_warnings", []) or [])
    risk_level = "medium" if approximate else "low"
    status = "warn" if approximate else "pass"
    message = (
        "Timecodes are approximate; review edl.review.html when footage feels out of sync with narration"
        if approximate
        else "Timecodes are strict/aligned enough for normal matching QA"
    )
    recommended_next_step = (
        "Use a forced aligner such as whisperx or smaller source windows before changing global audio delay"
        if approximate
        else None
    )
    return {
        "status": status,
        "risk_level": risk_level,
        "timecode_quality": quality,
        "approximate_timecodes": approximate,
        "asr_provider": film_meta.get("asr_provider"),
        "aligner_provider": film_meta.get("aligner_provider"),
        "source_language": film_meta.get("source_language"),
        "translate_mode": film_meta.get("translate_mode"),
        "speech_count": film_meta.get("speech_count"),
        "visual_count": film_meta.get("visual_count"),
        "asr_warning_count": len(asr_warnings),
        "asr_warnings": asr_warnings,
        "message": message,
        "recommended_next_step": recommended_next_step,
    }


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
    film_meta = read_json(meta_paths["ingest"])
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "stages": [stage.__dict__ for stage in stages],
        "timecode_qa": build_timecode_qa(film_meta),
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
