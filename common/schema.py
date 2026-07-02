from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SegmentType = Literal["speech", "visual"]
ProviderMode = Literal["auto", "ai33", "genmax"]


class FilmMapSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    type: SegmentType
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    ko: str | None = None
    en: str | None = None
    scene_desc: str | None = None

    @field_validator("ko", "en", "scene_desc")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_segment(self) -> "FilmMapSegment":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        if self.type == "speech":
            if not self.ko:
                raise ValueError("speech segment requires ko")
            if not self.en:
                raise ValueError("speech segment requires en")
            if self.scene_desc is not None:
                raise ValueError("speech segment requires scene_desc=null")
        if self.type == "visual":
            if not self.scene_desc:
                raise ValueError("visual segment requires scene_desc")
            if self.ko is not None or self.en is not None:
                raise ValueError("visual segment requires ko=null and en=null")
        return self


class FilmMapMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_path: str
    duration: float = Field(gt=0)
    created_at: datetime
    whisper_model: str
    translate_model: str
    vision_model: str
    gap_threshold: float = Field(ge=0)
    max_vision_frames: int = Field(ge=0)
    speech_count: int = Field(ge=0)
    visual_count: int = Field(ge=0)
    cache_hits: list[str] = Field(default_factory=list)
    warnings_count: int = Field(ge=0)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    ko: str

    @field_validator("ko")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("transcript text cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_timecode(self) -> "TranscriptSegment":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        return self


class TranslatedSegment(TranscriptSegment):
    en: str

    @field_validator("en")
    @classmethod
    def validate_translation(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("translation cannot be empty")
        return normalized


class SilentGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)

    @property
    def duration(self) -> float:
        return self.tc_end - self.tc_start

    @property
    def midpoint(self) -> float:
        return self.tc_start + (self.duration / 2)

    @model_validator(mode="after")
    def validate_timecode(self) -> "SilentGap":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        return self


class VisionSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gap_id: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    scene_desc: str

    @field_validator("scene_desc")
    @classmethod
    def validate_scene_desc(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scene_desc cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_timecode(self) -> "VisionSegment":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        return self


class ReviewBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    narration: str
    from_seg_id: int = Field(ge=0)
    to_seg_id: int = Field(ge=0)
    src_tc_start: float = Field(ge=0)
    src_tc_end: float = Field(gt=0)
    is_hook: bool = False

    @field_validator("narration")
    @classmethod
    def validate_narration(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("narration cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_beat(self) -> "ReviewBeat":
        if self.to_seg_id < self.from_seg_id:
            raise ValueError("to_seg_id must be >= from_seg_id")
        if self.src_tc_end <= self.src_tc_start:
            raise ValueError("src_tc_end must be greater than src_tc_start")
        return self


class ReviewMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    glossary: list[dict[str, Any]] = Field(default_factory=list)
    target_video_s: float = Field(gt=0)
    char_budget: int = Field(gt=0)
    est_total_chars: int = Field(ge=0)
    coverage_pct: float = Field(ge=0, le=1)
    qa_report: list[dict[str, Any]] = Field(default_factory=list)
    n_qa_iterations: int = Field(ge=0)
    model_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)
    cache_hits: list[str] = Field(default_factory=list)


class BeatTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    audio_path: str
    tl_start: float = Field(ge=0)
    tl_end: float = Field(gt=0)
    duration: float = Field(gt=0)

    @field_validator("audio_path")
    @classmethod
    def validate_audio_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("audio_path cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_timing(self) -> "BeatTiming":
        if self.tl_end <= self.tl_start:
            raise ValueError("tl_end must be greater than tl_start")
        if abs((self.tl_end - self.tl_start) - self.duration) > 1e-3:
            raise ValueError("tl_end must equal tl_start + duration")
        return self


class TtsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str
    provider_mode: ProviderMode
    model: str
    speed: float = Field(gt=0)
    inter_beat_pause_s: float = Field(ge=0)
    total_duration_s: float = Field(ge=0)
    film_duration_s: float | None = None
    real_ratio: float | None = None
    total_chars: int = Field(ge=0)
    est_cost: float = Field(ge=0)
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TtsManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    cache_key: str
    narration_hash: str
    provider: str
    voice_id: str
    model: str
    speed: float
    normalized: bool
    audio_path: str



class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    index: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    duration: float = Field(gt=0)
    thumb: str
    motion_score: float = Field(ge=0, le=1)
    face_count: int = Field(ge=0)
    face_area: float = Field(ge=0, le=1)
    brightness: float = Field(ge=0, le=1)
    is_usable: bool

    @field_validator("src", "thumb")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("path fields cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_shot(self) -> "Shot":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        if abs((self.tc_end - self.tc_start) - self.duration) > 1e-3:
            raise ValueError("duration must equal tc_end - tc_start")
        return self


class ShotsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    duration_s: float = Field(gt=0)
    n_shots: int = Field(ge=0)
    n_usable: int = Field(ge=0)
    detector: str
    feature_config: dict[str, Any] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

class EdlPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tl_start: float = Field(ge=0)
    tl_end: float = Field(gt=0)
    src: str
    src_in: float = Field(ge=0)
    src_out: float = Field(gt=0)
    beat_id: int = Field(ge=0)
    shot_index: int = Field(ge=0)
    reused: bool = False
    speed: float = Field(gt=0)

    @field_validator("src")
    @classmethod
    def validate_src(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("src cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_placement(self) -> "EdlPlacement":
        if self.tl_end <= self.tl_start:
            raise ValueError("tl_end must be greater than tl_start")
        if self.src_out <= self.src_in:
            raise ValueError("src_out must be greater than src_in")
        tl_duration = self.tl_end - self.tl_start
        src_duration = self.src_out - self.src_in
        if self.speed == 1.0 and abs(tl_duration - src_duration) > 0.02:
            raise ValueError("speed=1.0 placement must be 1:1 duration")
        return self


class EdlMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_duration_s: float = Field(ge=0)
    n_placements: int = Field(ge=0)
    n_beats_widened: int = Field(ge=0)
    n_reused: int = Field(ge=0)
    n_speedfit: int = Field(ge=0)
    avg_clip_len: float = Field(ge=0)
    coverage_ok: bool
    warnings: list[str] = Field(default_factory=list)
    seed: int
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)

class RenderMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    codec: str
    video_duration_s: float = Field(ge=0)
    audio_duration_s: float = Field(ge=0)
    duration_match: bool
    n_placements: int = Field(ge=0)
    n_temp_clips: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)

    @field_validator("codec")
    @classmethod
    def validate_codec(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("codec cannot be empty")
        return normalized
def validate_film_map(segments: list[FilmMapSegment], duration: float | None = None) -> list[FilmMapSegment]:
    ordered = sorted(segments, key=lambda item: (item.tc_start, item.tc_end, item.id))
    for expected_id, segment in enumerate(ordered):
        if segment.id != expected_id:
            raise ValueError(f"segment id must be continuous: expected {expected_id}, got {segment.id}")
        if duration is not None and segment.tc_end > duration + 1e-6:
            raise ValueError(f"segment #{segment.id} exceeds video duration")
        if expected_id > 0:
            previous = ordered[expected_id - 1]
            if segment.tc_start < previous.tc_end - 1e-6:
                raise ValueError(f"segment #{segment.id} overlaps segment #{previous.id}")
    return ordered


def validate_review_script(beats: list[ReviewBeat], film_map: list[FilmMapSegment]) -> list[ReviewBeat]:
    ordered = sorted(beats, key=lambda item: item.beat_id)
    by_id = {segment.id: segment for segment in film_map}
    previous_non_hook_start: float | None = None
    for expected_id, beat in enumerate(ordered):
        if beat.beat_id != expected_id:
            raise ValueError(f"beat_id must be continuous: expected {expected_id}, got {beat.beat_id}")
        if beat.from_seg_id not in by_id:
            raise ValueError(f"beat #{beat.beat_id} from_seg_id does not exist")
        if beat.to_seg_id not in by_id:
            raise ValueError(f"beat #{beat.beat_id} to_seg_id does not exist")
        source_start = by_id[beat.from_seg_id].tc_start
        source_end = by_id[beat.to_seg_id].tc_end
        if abs(beat.src_tc_start - source_start) > 1e-6:
            raise ValueError(f"beat #{beat.beat_id} src_tc_start does not match film_map")
        if abs(beat.src_tc_end - source_end) > 1e-6:
            raise ValueError(f"beat #{beat.beat_id} src_tc_end does not match film_map")
        if not beat.is_hook:
            if previous_non_hook_start is not None and beat.src_tc_start < previous_non_hook_start - 1e-6:
                raise ValueError(f"beat #{beat.beat_id} non-hook order is not monotonic")
            previous_non_hook_start = beat.src_tc_start
    if ordered and not ordered[0].is_hook:
        raise ValueError("first beat must be a hook")
    return ordered


def validate_beats_timing(timings: list[BeatTiming], pause_s: float = 0.0) -> list[BeatTiming]:
    ordered = sorted(timings, key=lambda item: item.beat_id)
    previous_end: float | None = None
    for expected_id, timing in enumerate(ordered):
        if timing.beat_id != expected_id:
            raise ValueError(f"beat timing ids must be continuous: expected {expected_id}, got {timing.beat_id}")
        if previous_end is not None:
            expected_start = previous_end + pause_s
            if abs(timing.tl_start - expected_start) > 1e-3:
                raise ValueError(f"beat #{timing.beat_id} tl_start does not match previous tl_end + pause")
        previous_end = timing.tl_end
    return ordered



def validate_shots(shots: list[Shot], duration: float | None = None) -> list[Shot]:
    ordered = sorted(shots, key=lambda item: (item.tc_start, item.tc_end, item.index))
    previous_start = -1.0
    for expected_index, shot in enumerate(ordered):
        if shot.index != expected_index:
            raise ValueError(f"shot index must be continuous: expected {expected_index}, got {shot.index}")
        if shot.tc_start < previous_start - 1e-6:
            raise ValueError(f"shot #{shot.index} is not sorted by tc_start")
        if duration is not None and shot.tc_end > duration + 1e-6:
            raise ValueError(f"shot #{shot.index} exceeds source duration")
        previous_start = shot.tc_start
    return ordered

def validate_edl(placements: list[EdlPlacement], total_duration: float | None = None) -> list[EdlPlacement]:
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id))
    previous_end = 0.0
    for index, placement in enumerate(ordered):
        if index == 0 and abs(placement.tl_start) > 0.05:
            raise ValueError("EDL must start at timeline 0")
        if index > 0 and abs(placement.tl_start - previous_end) > 0.05:
            raise ValueError(f"EDL has gap or overlap before placement #{index}")
        previous_end = placement.tl_end
    if total_duration is not None and ordered and abs(ordered[-1].tl_end - total_duration) > 0.05:
        raise ValueError("EDL final tl_end does not match total duration")
    return ordered
def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, BaseModel):
        text = data.model_dump_json(indent=2)
    elif isinstance(data, list) and all(isinstance(item, BaseModel) for item in data):
        text = "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in data) + "\n]"
    else:
        import json

        text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


