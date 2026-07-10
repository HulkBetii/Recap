from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def stable_id(value: object, prefix: str = "br") -> str:
    import json

    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


class BrollCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str
    beat_id: int = Field(ge=0)
    shot_index: int = Field(ge=0)
    tl_start: float = Field(ge=0)
    tl_end: float = Field(gt=0)
    src: str
    src_in: float = Field(ge=0)
    src_out: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    narration_preview: str
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rank_score: float = Field(ge=0)
    frame_src: str
    frame_tc: float = Field(ge=0)
    frame_shot_index: int = Field(ge=0)
    frame_shot_distance_used: int = Field(default=0, ge=0)
    frame_reason: str

    @field_validator("frame_id")
    @classmethod
    def validate_frame_id(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "_")
        if not normalized:
            raise ValueError("frame_id cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_ranges(self) -> "BrollCandidate":
        if self.tl_end <= self.tl_start:
            raise ValueError("tl_end must be greater than tl_start")
        if self.src_out <= self.src_in:
            raise ValueError("src_out must be greater than src_in")
        if abs((self.tl_end - self.tl_start) - self.duration_s) > 0.05:
            raise ValueError("duration_s must match timeline span")
        if self.frame_shot_index == self.shot_index:
            raise ValueError("frame_shot_index must differ from replaced shot_index")
        return self


class BrollPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["plan"] = "plan"
    source_edl: str
    source_shots: str
    max_replacement_ratio: float = Field(ge=0, le=1)
    max_broll_per_parent_beat: int = Field(ge=1)
    exclude_opening_s: float = Field(ge=0)
    min_broll_duration_s: float = Field(default=1.0, ge=0)
    min_frame_shot_distance: int = Field(default=3, ge=0)
    frame_reuse_window_s: float = Field(default=20.0, ge=0)
    n_skipped_short_duration: int = Field(default=0, ge=0)
    n_frame_keep_original_no_alternative: int = Field(default=0, ge=0)
    n_placements: int = Field(ge=0)
    n_candidates: int = Field(ge=0)
    target_replacements: int = Field(ge=0)
    original_footage_ratio_estimate: float = Field(ge=0, le=1)
    candidates: list[BrollCandidate]
    warnings: list[str] = Field(default_factory=list)


class BrollManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str
    frame_path: str | None = None
    clip_path: str | None = None
    source_tc: float = Field(ge=0)
    source_shot_index: int = Field(ge=0)
    status: Literal["generated", "failed"]
    duration_s: float = Field(ge=0)
    motion_preset: str
    warnings: list[str] = Field(default_factory=list)


class BrollQa(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    source_edl: str
    output_edl: str
    n_placements: int = Field(ge=0)
    n_planned: int = Field(ge=0)
    n_replaced: int = Field(ge=0)
    n_skipped_short_duration: int = Field(default=0, ge=0)
    n_frame_keep_original_no_alternative: int = Field(default=0, ge=0)
    frame_shot_distance_distribution: dict[str, int] = Field(default_factory=dict)
    n_extracted_frames: int = Field(ge=0)
    n_frame_fallbacks: int = Field(ge=0)
    n_failed_frames: int = Field(ge=0)
    replacement_ratio: float = Field(ge=0, le=1)
    original_footage_ratio_estimate: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)
    manifest: list[BrollManifestItem] = Field(default_factory=list)


def read_json(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
