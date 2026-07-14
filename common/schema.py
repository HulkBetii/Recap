from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SegmentType = Literal["speech", "visual"]
ProviderMode = Literal["auto", "ai33", "genmax", "openai"]
AsrProvider = Literal["faster-whisper", "openai-gpt4o", "openai-gpt4o-hybrid", "manual"]
AlignerProvider = Literal["none", "whisperx", "qwen3"]
TimecodeQuality = Literal["strict", "approximate"]
TranscriptCorrectionMode = Literal["off", "glossary", "openai"]
SourceLanguage = Literal["ko", "vi"]
TranslateMode = Literal["ko-en", "none"]

StorySectionType = Literal["setup", "inciting_incident", "conflict", "investigation", "reveal", "climax", "ending", "non_story"]
VisualIntent = Literal["character_intro", "dialogue", "location", "action", "reaction", "reveal", "transition", "ending"]
ChronologyMode = Literal["ordered", "flexible"]


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
    max_visual_gap_s: float = Field(ge=0, default=20.0)
    speech_count: int = Field(ge=0)
    visual_count: int = Field(ge=0)
    cache_hits: list[str] = Field(default_factory=list)
    warnings_count: int = Field(ge=0)
    asr_provider: AsrProvider = "faster-whisper"
    aligner_provider: AlignerProvider = "none"
    timecode_quality: TimecodeQuality = "strict"
    approximate_timecodes: bool = False
    asr_warnings: list[str] = Field(default_factory=list)
    transcript_correction_mode: TranscriptCorrectionMode = "off"
    transcript_correction_model: str | None = None
    transcript_correction_warnings: list[str] = Field(default_factory=list)
    source_language: SourceLanguage = "ko"
    translate_mode: TranslateMode = "ko-en"
    input_hash: str | None = None
    config_hash: str | None = None
    video_profile_hash: str | None = None
    cache_version: str | None = None

class TranscriptQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr_provider: AsrProvider
    aligner_provider: AlignerProvider = "none"
    timecode_quality: TimecodeQuality
    approximate_timecodes: bool
    warnings: list[str] = Field(default_factory=list)
    correction_mode: TranscriptCorrectionMode = "off"
    correction_model: str | None = None
    correction_warnings: list[str] = Field(default_factory=list)

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
    llm_backend: str = "chatgpt_playwright"
    video_profile_path: str | None = None
    n_non_story: int = Field(default=0, ge=0)
    intro_detection: dict[str, Any] | None = None
    story_start_s: float = Field(default=0.0, ge=0)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)
    cache_hits: list[str] = Field(default_factory=list)
    consistency_warnings: list[str] = Field(default_factory=list)
    style_preset: str | None = None
    style_strength: str | None = None
    style_sample_path: str | None = None
    style_qa_report: list[dict[str, Any]] = Field(default_factory=list)
    n_style_rewrites: int = Field(ge=0, default=0)
    readability_warnings: list[str] = Field(default_factory=list)
    n_non_story_beats_dropped: int = Field(default=0, ge=0)
    dropped_beat_ids: list[int] = Field(default_factory=list)
    non_story_filter_warnings: list[str] = Field(default_factory=list)
    content_type: str = "episode"
    hook_mode: str = "cold_open"
    target_ratio_mode: str = "fixed"
    auto_target_ratio: float | None = None
    complexity_score: float = Field(default=0.0, ge=0, le=1)
    opening_coherence_report: dict[str, Any] = Field(default_factory=dict)
    n_opening_rewrites: int = Field(default=0, ge=0)
    opening_warnings: list[str] = Field(default_factory=list)
    pre_story_dropped_beat_ids: list[int] = Field(default_factory=list)
    micro_beats_enabled: bool = False
    target_beat_audio_s: float | None = None
    max_beat_audio_s: float | None = None
    n_micro_beats_split: int = Field(default=0, ge=0)
    micro_beat_split_ids: list[int] = Field(default_factory=list)
    micro_beat_warnings: list[str] = Field(default_factory=list)
    qa_rewrite_limited: bool = False
    film_map_hash: str | None = None
    film_map_meta_hash: str | None = None
    story_map_hash: str | None = None
    video_profile_hash: str | None = None
    config_hash: str | None = None
    cache_version: str | None = None


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
    text_normalization: str = "vi"
    pronunciation_lexicon_path: str | None = None
    n_text_normalized: int = Field(default=0, ge=0)
    normalization_warnings: list[str] = Field(default_factory=list)
    pronunciation_qa_enabled: bool = True
    pronunciation_risk_count: int = Field(default=0, ge=0)
    pronunciation_suggest_backend: str = "off"
    pronunciation_warnings: list[str] = Field(default_factory=list)
    providers_used: list[str] = Field(default_factory=list)
    provider_counts: dict[str, int] = Field(default_factory=dict)
    fallback_count: int = Field(default=0, ge=0)
    openai_model: str | None = None
    openai_voice: str | None = None



class NonStoryRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    label: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("label cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> "NonStoryRange":
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

class IntroDetection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected: bool
    start_s: float = Field(default=0.0, ge=0)
    end_s: float | None = None
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_intro(self) -> "IntroDetection":
        if self.detected:
            if self.end_s is None:
                raise ValueError("detected intro requires end_s")
            if self.end_s <= self.start_s:
                raise ValueError("intro end_s must be greater than start_s")
        return self

class VideoProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_path: str
    duration_s: float = Field(gt=0)
    intro: IntroDetection
    non_story_ranges: list[NonStoryRange] = Field(default_factory=list)
    classifier: str
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)
    cache_hits: list[str] = Field(default_factory=list)
    input_hash: str | None = None
    config_hash: str | None = None
    cache_version: str | None = None


class StorySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: int = Field(ge=0)
    type: StorySectionType
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    segment_ids: list[int] = Field(default_factory=list)
    summary: str
    characters: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("summary cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_section(self) -> "StorySection":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        return self

class StoryMapMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    film_map_path: str
    video_profile_path: str | None = None
    content_type: str = "movie"
    duration_s: float = Field(gt=0)
    n_sections: int = Field(ge=0)
    n_non_story: int = Field(ge=0)
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    film_map_hash: str | None = None
    video_profile_hash: str | None = None
    config_hash: str | None = None
    cache_version: str | None = None

class ReviewIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    story_section_id: int | None = Field(default=None, ge=0)
    story_section_type: StorySectionType | None = None
    visual_intent: VisualIntent = "dialogue"
    chronology_mode: ChronologyMode = "flexible"
    visual_query_vi: str | None = None
    visual_query_en: str | None = None
    abstraction_class: str | None = None
    visual_explicitness: float | None = Field(default=None, ge=0, le=1)
    characters: list[dict[str, Any]] = Field(default_factory=list)
    action_cues: list[str] = Field(default_factory=list)
    emotion_cues: list[str] = Field(default_factory=list)
    location_cues: list[str] = Field(default_factory=list)
    object_cues: list[str] = Field(default_factory=list)
    negative_visual_cues: list[str] = Field(default_factory=list)
    preferred_shot_traits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("visual_query_vi", "visual_query_en", "abstraction_class")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

class ShotKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_path: str
    tc: float = Field(ge=0)
    role: str
    embedding_ref: str
    embedding_sha256: str | None = None

    @field_validator("frame_path", "role", "embedding_ref")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("shot keyframe text fields cannot be empty")
        return normalized

class ShotVisualIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_index: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    duration: float = Field(gt=0)
    is_story: bool = True
    is_usable: bool = True
    keyframes: list[ShotKeyframe] = Field(default_factory=list)
    shot_embedding_ref: str
    shot_embedding_sha256: str | None = None
    ocr_text: str | None = None
    ocr_score: float = Field(default=0.0, ge=0, le=1)
    title_like_prob: float = Field(default=0.0, ge=0, le=1)
    credit_like_prob: float = Field(default=0.0, ge=0, le=1)
    black_frame_ratio: float = Field(default=0.0, ge=0, le=1)
    face_tracks: list[dict[str, Any]] = Field(default_factory=list)
    visual_tags: list[str] = Field(default_factory=list)
    caption: str | None = None
    caption_confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("shot_embedding_ref")
    @classmethod
    def validate_embedding_ref(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("shot_embedding_ref cannot be empty")
        return normalized

    @field_validator("ocr_text", "caption")
    @classmethod
    def normalize_optional_shot_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_visual_index(self) -> "ShotVisualIndex":
        if self.tc_end <= self.tc_start:
            raise ValueError("tc_end must be greater than tc_start")
        if abs((self.tc_end - self.tc_start) - self.duration) > 1e-3:
            raise ValueError("duration must equal tc_end - tc_start")
        if not self.keyframes:
            raise ValueError("shot visual index requires at least one keyframe")
        return self

class VisualIndexMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    src: str
    embedding_mode: str
    embedding_model: str
    device: str
    embedding_dim: int = Field(ge=0)
    keyframes_per_shot: int = Field(gt=0)
    n_shots: int = Field(ge=0)
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    film_hash: str | None = None
    shots_hash: str | None = None
    config_hash: str | None = None
    preprocessing_version: str | None = None
    logit_scale: float | None = Field(default=None, gt=0)
    logit_bias: float | None = None

    @field_validator("src", "embedding_mode", "embedding_model", "device")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("visual index meta text fields cannot be empty")
        return normalized

class ShotVisualIndexFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: VisualIndexMeta
    shots: list[ShotVisualIndex] = Field(default_factory=list)


class VisualGoldenCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_index: int = Field(ge=0)
    base_score: float
    visual_score: float = Field(ge=0, le=1)
    drift_tier: int = Field(default=0, ge=0)
    source_drift_s: float = Field(default=0.0, ge=0)
    reused: bool = False
    duration_s: float = Field(default=3.0, gt=0)
    acceptable: bool = False


class VisualGoldenBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video: str = Field(min_length=1)
    beat_id: int = Field(ge=0)
    candidates: list[VisualGoldenCandidate]

    @model_validator(mode="after")
    def validate_candidates(self) -> "VisualGoldenBeat":
        if not self.candidates:
            raise ValueError("visual golden beat requires candidates")
        if not any(candidate.acceptable for candidate in self.candidates):
            raise ValueError("visual golden beat requires at least one acceptable candidate")
        return self


class VisualGoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    videos: list[str] = Field(min_length=2)
    beats: list[VisualGoldenBeat]

    @model_validator(mode="after")
    def validate_videos(self) -> "VisualGoldenSet":
        if len(set(self.videos)) != len(self.videos):
            raise ValueError("visual golden videos must be unique")
        beat_videos = {beat.video for beat in self.beats}
        unknown = sorted(beat_videos - set(self.videos))
        if unknown:
            raise ValueError(f"visual golden beats reference unknown videos: {unknown}")
        missing = sorted(set(self.videos) - beat_videos)
        if missing:
            raise ValueError(f"visual golden set requires labeled beats for every video: {missing}")
        return self

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
    unusable_reasons: list[str] = Field(default_factory=list)
    is_story: bool = True
    exclude_reason: str | None = None
    is_end_credit: bool = False
    credit_like_score: float = Field(default=0.0, ge=0, le=1)

    @field_validator("src", "thumb")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("path fields cannot be empty")
        return normalized

    @field_validator("unusable_reasons")
    @classmethod
    def validate_unusable_reasons(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

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
    video_profile_path: str | None = None
    video_profile_hash: str | None = None
    n_non_story: int = Field(default=0, ge=0)
    n_end_credit: int = Field(default=0, ge=0)
    intro_detection: dict[str, Any] | None = None
    story_start_s: float = Field(default=0.0, ge=0)
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
    n_intro_excluded: int = Field(default=0, ge=0)
    n_empty_beats: int = Field(default=0, ge=0)
    n_high_repeat_beats: int = Field(default=0, ge=0)
    n_dark_fallback_beats: int = Field(default=0, ge=0)
    n_end_credit_excluded: int = Field(default=0, ge=0)
    n_capacity_exhausted_beats: int = Field(default=0, ge=0)
    n_unused_source_reuse: int = Field(default=0, ge=0)
    n_overlapping_repeats: int = Field(default=0, ge=0)
    max_repeat_ratio: float = Field(default=0.0, ge=0)
    avg_clip_len: float = Field(ge=0)
    coverage_ok: bool
    warnings: list[str] = Field(default_factory=list)
    seed: int
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    algorithm_version: str = "1"

class RenderMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    codec: str
    video_duration_s: float = Field(ge=0)
    audio_duration_s: float = Field(ge=0)
    audio_delay_s: float = Field(default=0.0, ge=0)
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


REACTION_REMIX_SCHEMA_VERSION = "reaction-remix.v1"
REACTION_REMIX_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ReactionBlockKind = Literal["reaction", "commentary", "transition", "branding", "broll", "mixed", "unknown"]
ReactionCutKind = Literal["source_boundary", "silence_midpoint", "scene_boundary", "turn_boundary"]
ReactionCutSafetyMode = Literal["source_boundary", "full_handle", "word_edge", "overlap"]
ReactionAnalysisStatus = Literal["ok", "analysis_gap"]
RemixQaStatus = Literal["pass", "warn", "fail"]


def _validate_reaction_hash(value: str) -> str:
    if not REACTION_REMIX_HASH_PATTERN.fullmatch(value):
        raise ValueError("hash must be exactly 64 lowercase hexadecimal characters")
    return value


def _validate_reaction_path(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("path cannot be empty")
    if "\\" in normalized:
        raise ValueError("reaction-remix paths must use forward slashes")
    return normalized


class ReactionContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReactionStageMeta(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    stage: str
    algorithm_version: str
    input_hashes: dict[str, str] = Field(default_factory=dict)
    config_hash: str
    output_hashes: dict[str, str] = Field(default_factory=dict)
    cache_hits: list[str] = Field(default_factory=list)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("stage", "algorithm_version")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be empty")
        return normalized

    @field_validator("config_hash")
    @classmethod
    def validate_config_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)

    @field_validator("input_hashes", "output_hashes")
    @classmethod
    def validate_hash_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        for name, digest in value.items():
            if not name.strip():
                raise ValueError("hash mapping keys cannot be empty")
            _validate_reaction_hash(digest)
        return value


class ReactionVideoStream(ReactionContractModel):
    stream_index: int = Field(ge=0)
    codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    pixel_format: str
    frame_rate_mode: Literal["cfr", "vfr"]


class ReactionAudioStream(ReactionContractModel):
    stream_index: int = Field(ge=0)
    codec: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    channel_layout: str


class ReactionSubtitleStream(ReactionContractModel):
    stream_index: int = Field(ge=0)
    codec: str
    language: str | None = None
    title: str | None = None


class ReactionSource(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    input_path: str
    input_hash: str
    duration_s: float = Field(gt=0)
    video: ReactionVideoStream
    audio: ReactionAudioStream
    subtitle_streams: list[ReactionSubtitleStream] = Field(default_factory=list)
    has_burned_in_subtitles: bool
    subtitle_policy: Literal["burned_in_preserve"] = "burned_in_preserve"
    created_at: datetime
    config_hash: str
    warnings: list[str] = Field(default_factory=list)

    @field_validator("input_hash", "config_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)

    @field_validator("input_path")
    @classmethod
    def validate_input_path(cls, value: str) -> str:
        return _validate_reaction_path(value)

    @model_validator(mode="after")
    def validate_subtitle_streams(self) -> "ReactionSource":
        if self.subtitle_streams:
            raise ValueError("reaction-remix.v1 does not support soft subtitle streams")
        return self


class ReactionWord(ReactionContractModel):
    word_id: str
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    text: str
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_word(self) -> "ReactionWord":
        if self.tc_end <= self.tc_start:
            raise ValueError("word tc_end must be greater than tc_start")
        if not self.text.strip():
            raise ValueError("word text cannot be empty")
        return self


class ReactionAnalysisRegion(ReactionContractModel):
    region_id: str
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    status: ReactionAnalysisStatus
    attempts: int = Field(ge=1)
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_region(self) -> "ReactionAnalysisRegion":
        if self.tc_end <= self.tc_start:
            raise ValueError("region tc_end must be greater than tc_start")
        if self.status == "analysis_gap" and not self.error:
            raise ValueError("analysis_gap region requires error")
        if self.status == "ok" and self.error is not None:
            raise ValueError("ok region cannot contain error")
        return self


class ReactionTurn(ReactionContractModel):
    turn_id: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    text: str
    language: str
    language_confidence: float = Field(ge=0, le=1)
    speaker_id: str
    speaker_confidence: float = Field(ge=0, le=1)
    asr_confidence: float = Field(ge=0, le=1)
    region_id: str
    words: list[ReactionWord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_turn(self) -> "ReactionTurn":
        if self.tc_end <= self.tc_start:
            raise ValueError("turn tc_end must be greater than tc_start")
        if not self.text.strip():
            raise ValueError("turn text cannot be empty")
        previous_end = self.tc_start
        for word in self.words:
            if word.tc_start < self.tc_start - 1e-6 or word.tc_end > self.tc_end + 1e-6:
                raise ValueError("word timestamp must stay inside owning turn")
            if word.tc_start < previous_end - 1e-6:
                raise ValueError("word timestamps cannot overlap")
            previous_end = word.tc_end
        return self


class ReactionSpeakerCluster(ReactionContractModel):
    speaker_id: str
    region_count: int = Field(ge=1)
    total_duration_s: float = Field(gt=0)
    language_ratios: dict[str, float] = Field(default_factory=dict)
    narrator_candidate: bool = False
    confidence: float = Field(ge=0, le=1)

    @field_validator("language_ratios")
    @classmethod
    def validate_language_ratios(cls, value: dict[str, float]) -> dict[str, float]:
        if any(ratio < 0 or ratio > 1 for ratio in value.values()):
            raise ValueError("language ratios must be between 0 and 1")
        return value


class ReactionAsrInfo(ReactionContractModel):
    provider: Literal["faster-whisper"] = "faster-whisper"
    model: str = "large-v3"
    device: str
    chunk_s: float = Field(gt=0)
    overlap_s: float = Field(ge=0)
    language_mode: Literal["auto"] = "auto"
    word_timestamps: Literal[True] = True


class ReactionTranscript(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    source_duration_s: float = Field(gt=0)
    regions: list[ReactionAnalysisRegion]
    turns: list[ReactionTurn]
    speaker_clusters: list[ReactionSpeakerCluster] = Field(default_factory=list)
    narrator_speaker_id: str | None = None
    asr: ReactionAsrInfo
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class ReactionCutPoint(ReactionContractModel):
    cut_point_id: str
    tc: float = Field(ge=0)
    kind: ReactionCutKind
    confidence: float = Field(ge=0, le=1)
    speech_padding_s: float = Field(ge=0)
    safety_mode: ReactionCutSafetyMode | None = None
    left_handle_s: float | None = None
    right_handle_s: float | None = None

    @model_validator(mode="after")
    def validate_safety(self) -> "ReactionCutPoint":
        if self.safety_mode is None:
            if (self.left_handle_s is None) != (self.right_handle_s is None):
                raise ValueError("boundary handle values must be both present or both omitted")
            if self.left_handle_s is None:
                return self
            if self.kind == "source_boundary":
                raise ValueError("source boundary cuts require source_boundary safety mode")
            if self.left_handle_s < -1e-6 or self.right_handle_s < -1e-6:
                raise ValueError("protected insufficient-handle edges cannot overlap adjacent content")
            if self.confidence > 0.89 + 1e-6:
                raise ValueError("protected insufficient-handle edge confidence cannot exceed 0.89")
            return self
        if self.left_handle_s is None or self.right_handle_s is None:
            raise ValueError("boundary safety mode requires left and right handle values")
        if self.safety_mode == "source_boundary":
            if self.kind != "source_boundary":
                raise ValueError("source_boundary safety mode requires a source boundary cut")
            return self
        if self.kind == "source_boundary":
            raise ValueError("source boundary cuts require source_boundary safety mode")
        if self.safety_mode == "full_handle" and (
            self.left_handle_s < self.speech_padding_s - 1e-6
            or self.right_handle_s < self.speech_padding_s - 1e-6
        ):
            raise ValueError("full-handle cut safety requires speech handles on both sides")
        if self.safety_mode == "word_edge":
            if self.left_handle_s < -1e-6 or self.right_handle_s < -1e-6:
                raise ValueError("word-edge cut safety cannot overlap adjacent content")
            if abs(self.confidence - 0.90) > 1e-6:
                raise ValueError("word-edge cut confidence must equal 0.90")
        if self.safety_mode == "overlap":
            if self.left_handle_s >= -1e-6 and self.right_handle_s >= -1e-6:
                raise ValueError("overlap safety requires a negative content handle")
            if self.confidence > 0.89 + 1e-6:
                raise ValueError("overlap cut confidence cannot exceed 0.89")
        return self


class ReactionBlockSemantic(ReactionContractModel):
    summary_ja: str | None = None
    country: str | None = None
    topic: str | None = None
    sentiment: str | None = None
    intensity: float | None = Field(default=None, ge=0, le=1)
    novelty: float | None = Field(default=None, ge=0, le=1)


class ReactionPreservation(ReactionContractModel):
    video: Literal["source_frames"] = "source_frames"
    audio: Literal["source_mix", "replace_commentary"]
    speed: Literal[1.0] = 1.0
    allow_trim_to_safe_cut_points: bool = True


class ReactionBlock(ReactionContractModel):
    block_id: str
    kind: ReactionBlockKind
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    content_tc_start: float = Field(ge=0)
    content_tc_end: float = Field(gt=0)
    start_cut_point_id: str
    end_cut_point_id: str
    turn_ids: list[int] = Field(default_factory=list)
    language_codes: list[str] = Field(default_factory=list)
    speaker_ids: list[str] = Field(default_factory=list)
    sequence_group_id: str | None = None
    sequence_index: int | None = Field(default=None, ge=0)
    semantic: ReactionBlockSemantic | None = None
    preservation: ReactionPreservation
    eligible_commentary_visual: bool
    classification_confidence: float = Field(ge=0, le=1)
    language_confidence: float = Field(ge=0, le=1)
    speaker_confidence: float = Field(ge=0, le=1)
    boundary_confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("block_id")
    @classmethod
    def validate_block_id(cls, value: str) -> str:
        if not re.fullmatch(r"block-\d{4,}", value):
            raise ValueError("block_id must match block-0001")
        return value

    @model_validator(mode="after")
    def validate_block(self) -> "ReactionBlock":
        if self.tc_end <= self.tc_start:
            raise ValueError("block tc_end must be greater than tc_start")
        if self.content_tc_end <= self.content_tc_start:
            raise ValueError("content_tc_end must be greater than content_tc_start")
        if self.content_tc_start < self.tc_start - 1e-6 or self.content_tc_end > self.tc_end + 1e-6:
            raise ValueError("content span must stay inside safe media span")
        if (self.sequence_group_id is None) != (self.sequence_index is None):
            raise ValueError("sequence_group_id and sequence_index must be set together")
        if self.kind in {"reaction", "mixed", "unknown"} and self.preservation.audio != "source_mix":
            raise ValueError(f"{self.kind} block must preserve source_mix audio")
        if self.kind == "commentary" and self.preservation.audio != "replace_commentary":
            raise ValueError("commentary block must replace source commentary audio")
        if self.kind == "commentary" and min(
            self.language_confidence, self.speaker_confidence, self.boundary_confidence
        ) < 0.90:
            raise ValueError("commentary classification requires all confidence signals >= 0.90")
        return self


class ReactionBlocks(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    transcript_hash: str
    source_duration_s: float = Field(gt=0)
    cut_points: list[ReactionCutPoint]
    blocks: list[ReactionBlock]
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "transcript_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixDurationPolicy(ReactionContractModel):
    hard_min_output_ratio: float = Field(default=0.80, ge=0, le=1)
    preferred_min_output_ratio: float = Field(default=0.85, ge=0, le=1)
    preferred_max_output_ratio: float = Field(default=0.90, ge=0, le=1)
    hard_max_output_ratio: float = Field(default=1.0, ge=0, le=1)
    target_duration_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_ratios(self) -> "RemixDurationPolicy":
        if not (
            self.hard_min_output_ratio
            <= self.preferred_min_output_ratio
            <= self.preferred_max_output_ratio
            <= self.hard_max_output_ratio
        ):
            raise ValueError("duration ratios must be ordered hard_min <= preferred_min <= preferred_max <= hard_max")
        return self


class RemixPlanItem(ReactionContractModel):
    item_id: str
    order: int = Field(ge=0)
    kind: Literal["source_block", "commentary_slot"]
    role: str
    block_id: str | None = None
    slot_id: str | None = None
    start_cut_point_id: str | None = None
    end_cut_point_id: str | None = None
    evidence_block_ids: list[str] = Field(default_factory=list)
    preferred_visual_block_ids: list[str] = Field(default_factory=list)
    target_duration_s: float | None = Field(default=None, gt=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    char_budget: int | None = Field(default=None, gt=0)
    dependency_group_id: str | None = None
    reason: str

    @model_validator(mode="after")
    def validate_item(self) -> "RemixPlanItem":
        if self.kind == "source_block":
            if not self.block_id or not self.start_cut_point_id or not self.end_cut_point_id:
                raise ValueError("source_block item requires block and cut point IDs")
            if self.slot_id is not None or self.target_duration_s is not None or self.max_duration_s is not None:
                raise ValueError("source_block item cannot contain commentary slot fields")
        else:
            if not self.slot_id or not self.evidence_block_ids:
                raise ValueError("commentary_slot item requires slot_id and evidence_block_ids")
            if self.block_id is not None or self.start_cut_point_id is not None or self.end_cut_point_id is not None:
                raise ValueError("commentary_slot item cannot reference a source span directly")
            if self.target_duration_s is None or self.max_duration_s is None or self.char_budget is None:
                raise ValueError("commentary_slot item requires duration and character budgets")
            if len(self.preferred_visual_block_ids) != 1:
                raise ValueError("commentary_slot item requires exactly one preferred visual block")
        return self


class RemixExcludedBlock(ReactionContractModel):
    block_id: str
    reason: str
    category: Literal["commentary", "transition", "branding", "broll", "duplicate_reaction", "manual_drop", "other"]
    source_duration_s: float = Field(gt=0)
    unique_reaction_speech_s: float = Field(default=0.0, ge=0)


class RemixSemanticAnnotation(ReactionContractModel):
    block_id: str
    summary_ja: str | None = None
    country: str | None = None
    topic: str | None = None
    sentiment: str | None = None
    intensity: float | None = Field(default=None, ge=0, le=1)
    novelty: float | None = Field(default=None, ge=0, le=1)


class RemixRetention(ReactionContractModel):
    unique_reaction_speech_ratio: float = Field(ge=0, le=1)
    reaction_block_ratio: float = Field(ge=0, le=1)
    country_coverage_ratio: float = Field(ge=0, le=1)
    topic_coverage_ratio: float = Field(ge=0, le=1)


class ReactionLlmInfo(ReactionContractModel):
    backend: Literal["chatgpt_playwright"] = "chatgpt_playwright"
    session_url: str | None = None
    attempts: int = Field(ge=1)


class RemixPlan(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    blocks_hash: str
    original_duration_s: float = Field(gt=0)
    duration_policy: RemixDurationPolicy
    items: list[RemixPlanItem]
    excluded_blocks: list[RemixExcludedBlock] = Field(default_factory=list)
    semantic_annotations: list[RemixSemanticAnnotation] = Field(default_factory=list)
    predicted_duration_s: float = Field(gt=0)
    predicted_output_ratio: float = Field(gt=0)
    retention: RemixRetention
    llm: ReactionLlmInfo
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "blocks_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class CommentarySlotQa(ReactionContractModel):
    language_ok: bool
    evidence_ok: bool
    style_ok: bool
    length_ok: bool


class CommentaryScriptSlot(ReactionContractModel):
    slot_id: str
    before_item_id: str | None = None
    after_item_id: str | None = None
    role: str
    text_ja: str
    evidence_block_ids: list[str]
    target_duration_s: float = Field(gt=0)
    max_duration_s: float = Field(gt=0)
    char_budget: int = Field(gt=0)
    tone_tags: list[str] = Field(default_factory=list)
    qa: CommentarySlotQa
    warnings: list[str] = Field(default_factory=list)

    @field_validator("text_ja")
    @classmethod
    def validate_text_ja(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text_ja cannot be empty")
        return normalized


class CommentaryScript(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    plan_hash: str
    language: Literal["ja"] = "ja"
    style_id: str
    slots: list[CommentaryScriptSlot]
    llm: ReactionLlmInfo
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "plan_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class CommentaryFitRequest(ReactionContractModel):
    slot_id: str
    actual_duration_s: float = Field(gt=0)
    target_duration_s: float = Field(gt=0)
    max_duration_s: float = Field(gt=0)
    tolerance_s: float = Field(gt=0)
    direction: Literal["shorten", "lengthen", "clarify"]
    attempt: int = Field(ge=1, le=2)
    reason: str


class CommentaryFitRequests(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    script_hash: str
    requests: list[CommentaryFitRequest]
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "script_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class CommentaryVoicePolicy(ReactionContractModel):
    provider: Literal["ai33"] = "ai33"
    voice_id: Literal["elevenlabs_QPtBgsg1dxKTQHNpHrHt"] = "elevenlabs_QPtBgsg1dxKTQHNpHrHt"
    model: Literal["eleven_multilingual_v2"] = "eleven_multilingual_v2"
    speed: Literal[1.0] = 1.0
    fallback_provider: None = None
    text_normalization: Literal["ja_basic"] = "ja_basic"


class CommentaryAudioItem(ReactionContractModel):
    slot_id: str
    audio_path: str
    duration_s: float = Field(gt=0)
    provider: Literal["ai33"] = "ai33"
    voice_id: Literal["elevenlabs_QPtBgsg1dxKTQHNpHrHt"] = "elevenlabs_QPtBgsg1dxKTQHNpHrHt"
    model: Literal["eleven_multilingual_v2"] = "eleven_multilingual_v2"
    speed: Literal[1.0] = 1.0
    text_hash: str
    cache_key: str
    audio_sha256: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    normalized: bool
    lufs_i: float
    true_peak_dbfs: float
    asr_text_match: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("text_hash", "cache_key")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)

    @field_validator("audio_sha256")
    @classmethod
    def validate_optional_audio_hash(cls, value: str | None) -> str | None:
        return _validate_reaction_hash(value) if value is not None else None

    @field_validator("audio_path")
    @classmethod
    def validate_audio_path(cls, value: str) -> str:
        return _validate_reaction_path(value)


class CommentaryAudio(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    script_hash: str
    voice_policy: CommentaryVoicePolicy
    items: list[CommentaryAudioItem]
    total_commentary_duration_s: float = Field(ge=0)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "script_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class AudioAsset(ReactionContractModel):
    asset_id: str
    kind: Literal["no_vocals", "source_mix", "tts"]
    path: str
    content_hash: str
    source_hash: str
    duration_s: float = Field(gt=0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    src_tc_start: float | None = Field(default=None, ge=0)
    src_tc_end: float | None = Field(default=None, gt=0)
    leakage_detected: bool = False
    warnings: list[str] = Field(default_factory=list)

    @field_validator("content_hash", "source_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_reaction_path(value)

    @model_validator(mode="after")
    def validate_span(self) -> "AudioAsset":
        if (self.src_tc_start is None) != (self.src_tc_end is None):
            raise ValueError("audio asset source span must set both start and end")
        if self.src_tc_start is not None and self.src_tc_end is not None and self.src_tc_end <= self.src_tc_start:
            raise ValueError("audio asset src_tc_end must be greater than src_tc_start")
        return self


class AudioAssets(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    items: list[AudioAsset]
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixOutputSpec(ReactionContractModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    audio_sample_rate: int = Field(gt=0)
    audio_channels: int = Field(gt=0)


class RemixVisualPolicy(ReactionContractModel):
    mask_subtitles: Literal[False] = False
    add_subtitles: Literal[False] = False
    add_text: Literal[False] = False
    blur: Literal[False] = False
    overlay: Literal[False] = False
    preserve_burned_in_pixels: Literal[True] = True


class RemixVideoPlacement(ReactionContractModel):
    src: str
    src_in: float = Field(ge=0)
    src_out: float = Field(gt=0)
    speed: Literal[1.0] = 1.0
    filters: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("src")
    @classmethod
    def validate_src(cls, value: str) -> str:
        return _validate_reaction_path(value)

    @model_validator(mode="after")
    def validate_span(self) -> "RemixVideoPlacement":
        if self.src_out <= self.src_in:
            raise ValueError("video src_out must be greater than src_in")
        return self


class RemixAudioPlacement(ReactionContractModel):
    mode: Literal["source", "tts", "tts_bed", "silence"]
    source_src: str | None = None
    source_in: float | None = Field(default=None, ge=0)
    source_out: float | None = Field(default=None, gt=0)
    source_gain_db: float | None = None
    tts_audio_path: str | None = None
    tts_gain_db: float | None = None
    bed_audio_path: str | None = None
    bed_in: float | None = Field(default=None, ge=0)
    bed_out: float | None = Field(default=None, gt=0)
    bed_gain_db: float | None = None
    filters: list[str] = Field(default_factory=list)

    @field_validator("source_src", "tts_audio_path", "bed_audio_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return _validate_reaction_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_mode(self) -> "RemixAudioPlacement":
        if self.mode == "source":
            if None in {self.source_src, self.source_in, self.source_out, self.source_gain_db}:
                raise ValueError("source audio mode requires source path, span, and gain")
            if self.source_gain_db != 0.0 or self.filters:
                raise ValueError("source audio must use 0 dB gain and no filters")
            if self.tts_audio_path is not None or self.bed_audio_path is not None:
                raise ValueError("source audio cannot include TTS or bed")
        elif self.mode == "tts":
            if not self.tts_audio_path or self.tts_gain_db is None:
                raise ValueError("tts mode requires TTS path and gain")
            if self.source_src is not None or self.bed_audio_path is not None:
                raise ValueError("tts mode cannot include source audio or bed")
        elif self.mode == "tts_bed":
            if not self.tts_audio_path or self.tts_gain_db is None or not self.bed_audio_path:
                raise ValueError("tts_bed mode requires TTS and bed")
            if None in {self.bed_in, self.bed_out, self.bed_gain_db}:
                raise ValueError("tts_bed mode requires bed span and gain")
            if self.source_src is not None:
                raise ValueError("tts_bed mode cannot include source mix")
        else:
            if any(value is not None for value in (
                self.source_src, self.source_in, self.source_out, self.source_gain_db,
                self.tts_audio_path, self.tts_gain_db, self.bed_audio_path, self.bed_in,
                self.bed_out, self.bed_gain_db,
            )):
                raise ValueError("silence mode cannot reference audio assets")
        return self


class RemixPlacement(ReactionContractModel):
    placement_id: str
    item_id: str
    kind: Literal["reaction", "commentary", "transition", "branding", "broll", "mixed", "unknown"]
    origin_block_id: str
    tl_start: float = Field(ge=0)
    tl_end: float = Field(gt=0)
    video: RemixVideoPlacement
    audio: RemixAudioPlacement
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_placement(self) -> "RemixPlacement":
        if self.tl_end <= self.tl_start:
            raise ValueError("placement tl_end must be greater than tl_start")
        duration = self.video.src_out - self.video.src_in
        if abs((self.tl_end - self.tl_start) - duration) > 1e-3:
            raise ValueError("placement timeline duration must equal source video duration at 1.0x")
        if self.kind in {"reaction", "mixed", "unknown"}:
            if self.audio.mode != "source":
                raise ValueError(f"{self.kind} placement must preserve source audio")
            if abs((self.audio.source_in or 0.0) - self.video.src_in) > 1e-6 or abs(
                (self.audio.source_out or 0.0) - self.video.src_out
            ) > 1e-6:
                raise ValueError("reaction source video and audio spans must match")
        if self.kind == "commentary" and self.audio.mode == "source":
            raise ValueError("commentary placement cannot use original source mix")
        return self


class RemixEdl(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    plan_hash: str
    commentary_audio_hash: str
    output: RemixOutputSpec
    visual_policy: RemixVisualPolicy
    placements: list[RemixPlacement]
    total_duration_s: float = Field(ge=0)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "plan_hash", "commentary_audio_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixRepairRequestItem(ReactionContractModel):
    repair_id: str
    kind: Literal["duration_restore", "tts_fit", "bed_leakage", "reaction_media_mismatch"]
    affected_ids: list[str]
    reason: str
    attempt: int = Field(ge=1, le=2)
    requested_stage: Literal["plan", "write", "tts", "compose", "render"]


class RemixRepairRequests(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    items: list[RemixRepairRequestItem]
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixRenderTimelinePlacement(ReactionContractModel):
    placement_id: str
    tl_start_frame: int = Field(ge=0)
    tl_end_frame: int = Field(gt=0)
    tl_start_sample: int = Field(ge=0)
    tl_end_sample: int = Field(gt=0)
    src_start_frame: int = Field(ge=0)
    src_end_frame: int = Field(gt=0)
    src_start_sample: int = Field(ge=0)
    src_end_sample: int = Field(gt=0)


class RemixRenderTimeline(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    edl_hash: str
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    audio_sample_rate: int = Field(gt=0)
    placements: list[RemixRenderTimelinePlacement]
    total_frames: int = Field(gt=0)
    total_samples: int = Field(gt=0)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "edl_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixCommand(ReactionContractModel):
    command_id: str
    purpose: str
    args: list[str]


class RemixCommandManifest(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    edl_hash: str
    denylist: list[str]
    commands: list[RemixCommand]
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_hash", "edl_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixRenderMeta(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    edl_hash: str
    output_path: str
    video_codec: str
    audio_codec: str
    crf: int = Field(ge=0)
    audio_bitrate: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    audio_sample_rate: int = Field(gt=0)
    audio_channels: int = Field(gt=0)
    duration_s: float = Field(gt=0)
    n_placements: int = Field(ge=0)
    decode_ok: bool
    timeline_hash: str
    command_manifest_hash: str
    created_at: datetime
    cache_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, value: str) -> str:
        return _validate_reaction_path(value)

    @field_validator("source_hash", "edl_hash", "timeline_hash", "command_manifest_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


class RemixQaDuration(ReactionContractModel):
    source_s: float = Field(gt=0)
    output_s: float = Field(gt=0)
    output_ratio: float = Field(gt=0)
    hard_min_ratio: float = Field(ge=0, le=1)
    preferred_range: tuple[float, float]
    status: RemixQaStatus


class RemixQaReactionPreservation(ReactionContractModel):
    placements_checked: int = Field(ge=0)
    speed_mismatches: int = Field(ge=0)
    gain_mismatches: int = Field(ge=0)
    span_mismatches: int = Field(ge=0)
    failed_placement_ids: list[str] = Field(default_factory=list)
    max_gain_delta_db: float = Field(ge=0)
    min_audio_correlation: float = Field(ge=-1, le=1)
    max_av_drift_ms: float = Field(ge=0)
    min_sample_frame_similarity: float = Field(ge=0, le=1)
    status: RemixQaStatus

    @field_validator("failed_placement_ids")
    @classmethod
    def validate_failed_placement_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("failed reaction preservation placement IDs must be unique")
        return value


class RemixQaCommentary(ReactionContractModel):
    slots_checked: int = Field(ge=0)
    provider_mismatches: int = Field(ge=0)
    voice_mismatches: int = Field(ge=0)
    old_narrator_leakage_count: int = Field(ge=0)
    old_narrator_leakage_slot_ids: list[str] = Field(default_factory=list)
    protected_narrator_overlap_block_ids: list[str] = Field(default_factory=list)
    min_asr_text_match: float = Field(ge=0, le=1)
    status: RemixQaStatus

    @model_validator(mode="after")
    def validate_leakage_slots(self) -> "RemixQaCommentary":
        if len(self.old_narrator_leakage_slot_ids) != len(set(self.old_narrator_leakage_slot_ids)):
            raise ValueError("old narrator leakage slot IDs must be unique")
        if self.old_narrator_leakage_slot_ids and (
            len(self.old_narrator_leakage_slot_ids) != self.old_narrator_leakage_count
        ):
            raise ValueError("old narrator leakage count must match localized slot IDs")
        if len(self.protected_narrator_overlap_block_ids) != len(
            set(self.protected_narrator_overlap_block_ids)
        ):
            raise ValueError("protected narrator overlap block IDs must be unique")
        return self


class RemixQaVisualPolicy(ReactionContractModel):
    mask_operations: int = Field(ge=0)
    subtitle_additions: int = Field(ge=0)
    text_overlays: int = Field(ge=0)
    blur_operations: int = Field(ge=0)
    other_overlays: int = Field(ge=0)
    status: RemixQaStatus


class RemixQaAudio(ReactionContractModel):
    unexpected_silence_count: int = Field(ge=0)
    boundary_click_count: int = Field(ge=0)
    max_commentary_true_peak_dbfs: float
    full_output_true_peak_dbfs: float
    source_true_peak_dbfs: float
    peak_increase_db: float
    status: RemixQaStatus


class RemixQaTimeline(ReactionContractModel):
    gap_count: int = Field(ge=0)
    overlap_count: int = Field(ge=0)
    decode_ok: bool
    expected_frame_count: int | None = Field(default=None, ge=0)
    actual_frame_count: int | None = Field(default=None, ge=0)
    frame_count_delta: int | None = None
    expected_sample_count: int | None = Field(default=None, ge=0)
    actual_sample_count: int | None = Field(default=None, ge=0)
    sample_count_delta: int | None = None
    status: RemixQaStatus

    @model_validator(mode="after")
    def validate_decoded_counts(self) -> "RemixQaTimeline":
        frame_values = (self.expected_frame_count, self.actual_frame_count, self.frame_count_delta)
        sample_values = (self.expected_sample_count, self.actual_sample_count, self.sample_count_delta)
        if any(value is not None for value in frame_values) and any(value is None for value in frame_values):
            raise ValueError("decoded frame count measurement must set expected, actual, and delta")
        if any(value is not None for value in sample_values) and any(value is None for value in sample_values):
            raise ValueError("decoded sample count measurement must set expected, actual, and delta")
        if self.frame_count_delta is not None and self.frame_count_delta != self.actual_frame_count - self.expected_frame_count:  # type: ignore[operator]
            raise ValueError("frame_count_delta must equal actual_frame_count - expected_frame_count")
        if self.sample_count_delta is not None and self.sample_count_delta != self.actual_sample_count - self.expected_sample_count:  # type: ignore[operator]
            raise ValueError("sample_count_delta must equal actual_sample_count - expected_sample_count")
        return self


class RemixQaRepair(ReactionContractModel):
    kind: str
    affected_ids: list[str]
    attempt: int = Field(ge=1, le=2)
    previous_result: str
    new_result: str


class RemixQa(ReactionContractModel):
    schema_version: Literal["reaction-remix.v1"] = REACTION_REMIX_SCHEMA_VERSION
    source_hash: str
    edl_hash: str
    output_path: str
    status: Literal["pass", "fail"]
    duration: RemixQaDuration
    reaction_preservation: RemixQaReactionPreservation
    commentary: RemixQaCommentary
    visual_policy: RemixQaVisualPolicy
    audio: RemixQaAudio
    timeline: RemixQaTimeline
    repairs: list[RemixQaRepair] = Field(default_factory=list)
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, value: str) -> str:
        return _validate_reaction_path(value)

    @field_validator("source_hash", "edl_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_reaction_hash(value)


def validate_reaction_source(source: ReactionSource) -> ReactionSource:
    if source.subtitle_streams:
        raise ValueError("reaction-remix.v1 does not support soft subtitle streams")
    return source


def validate_reaction_transcript(transcript: ReactionTranscript) -> ReactionTranscript:
    region_ids = [region.region_id for region in transcript.regions]
    if len(region_ids) != len(set(region_ids)):
        raise ValueError("analysis region IDs must be unique")
    for region in transcript.regions:
        if region.tc_end > transcript.source_duration_s + 1e-6:
            raise ValueError(f"analysis region {region.region_id} exceeds source duration")

    turn_ids = [turn.turn_id for turn in transcript.turns]
    if len(turn_ids) != len(set(turn_ids)):
        raise ValueError("turn IDs must be unique")
    known_regions = set(region_ids)
    ordered_turns = sorted(transcript.turns, key=lambda item: (item.tc_start, item.tc_end, item.turn_id))
    previous_end = 0.0
    for turn in ordered_turns:
        if turn.region_id not in known_regions:
            raise ValueError(f"turn #{turn.turn_id} references unknown analysis region")
        if turn.tc_end > transcript.source_duration_s + 1e-6:
            raise ValueError(f"turn #{turn.turn_id} exceeds source duration")
        if turn.tc_start < previous_end - 1e-6:
            raise ValueError(f"turn #{turn.turn_id} overlaps previous primary turn")
        previous_end = turn.tc_end

    speaker_ids = [cluster.speaker_id for cluster in transcript.speaker_clusters]
    if len(speaker_ids) != len(set(speaker_ids)):
        raise ValueError("speaker cluster IDs must be unique")
    if transcript.narrator_speaker_id is not None and transcript.narrator_speaker_id not in set(speaker_ids):
        raise ValueError("narrator_speaker_id must reference a declared speaker cluster")
    return transcript.model_copy(update={"turns": ordered_turns})


def validate_reaction_blocks(blocks_file: ReactionBlocks, transcript: ReactionTranscript) -> ReactionBlocks:
    if blocks_file.source_hash != transcript.source_hash:
        raise ValueError("reaction blocks source hash does not match transcript")
    if abs(blocks_file.source_duration_s - transcript.source_duration_s) > 1e-3:
        raise ValueError("reaction blocks source duration does not match transcript")

    cut_ids = [cut.cut_point_id for cut in blocks_file.cut_points]
    if len(cut_ids) != len(set(cut_ids)):
        raise ValueError("cut point IDs must be unique")
    ordered_cuts = sorted(blocks_file.cut_points, key=lambda item: (item.tc, item.cut_point_id))
    if [cut.cut_point_id for cut in ordered_cuts] != cut_ids:
        raise ValueError("cut points must be sorted by time")
    cut_by_id = {cut.cut_point_id: cut for cut in ordered_cuts}
    for cut in ordered_cuts:
        if cut.tc > blocks_file.source_duration_s + 1e-6:
            raise ValueError(f"cut point {cut.cut_point_id} exceeds source duration")
        for turn in transcript.turns:
            for word in turn.words:
                if word.tc_start + 1e-6 < cut.tc < word.tc_end - 1e-6:
                    raise ValueError(f"cut point {cut.cut_point_id} cuts through word {word.word_id}")

    turn_by_id = {turn.turn_id: turn for turn in transcript.turns}
    ordered_blocks = sorted(blocks_file.blocks, key=lambda item: (item.tc_start, item.tc_end, item.block_id))
    if not ordered_blocks:
        raise ValueError("reaction blocks cannot be empty")
    for index, block in enumerate(ordered_blocks, start=1):
        expected_id = f"block-{index:04d}"
        if block.block_id != expected_id:
            raise ValueError(f"block ID must be stable and contiguous: expected {expected_id}")
        if block.start_cut_point_id not in cut_by_id or block.end_cut_point_id not in cut_by_id:
            raise ValueError(f"block {block.block_id} references unknown cut point")
        if abs(block.tc_start - cut_by_id[block.start_cut_point_id].tc) > 1e-6:
            raise ValueError(f"block {block.block_id} tc_start must derive from its cut point")
        if abs(block.tc_end - cut_by_id[block.end_cut_point_id].tc) > 1e-6:
            raise ValueError(f"block {block.block_id} tc_end must derive from its cut point")
        if index == 1 and abs(block.tc_start) > 1e-3:
            raise ValueError("reaction blocks must start at source time zero")
        if index > 1 and abs(block.tc_start - ordered_blocks[index - 2].tc_end) > 1e-3:
            raise ValueError(f"reaction blocks have a gap or overlap before {block.block_id}")
        for turn_id in block.turn_ids:
            turn = turn_by_id.get(turn_id)
            if turn is None:
                raise ValueError(f"block {block.block_id} references unknown turn #{turn_id}")
            if turn.words:
                if any(
                    word.tc_start < block.content_tc_start - 1e-6
                    or word.tc_end > block.content_tc_end + 1e-6
                    for word in turn.words
                ):
                    raise ValueError(f"turn #{turn_id} has words outside block {block.block_id} content span")
            elif turn.tc_start < block.content_tc_start - 1e-6 or turn.tc_end > block.content_tc_end + 1e-6:
                raise ValueError(f"turn #{turn_id} lies outside block {block.block_id} content span")
        overlaps_analysis_gap = any(
            region.status == "analysis_gap"
            and region.tc_start < block.tc_end - 1e-6
            and region.tc_end > block.tc_start + 1e-6
            for region in transcript.regions
        )
        if overlaps_analysis_gap and block.kind != "unknown":
            raise ValueError(f"block {block.block_id} overlaps analysis_gap and must be unknown")
        if block.kind == "commentary":
            boundary_modes = {
                cut_by_id[block.start_cut_point_id].safety_mode,
                cut_by_id[block.end_cut_point_id].safety_mode,
            }
            if None not in boundary_modes and not boundary_modes.issubset({"full_handle", "word_edge"}):
                raise ValueError(
                    f"commentary block {block.block_id} requires full_handle or word_edge boundaries"
                )
    if abs(ordered_blocks[-1].tc_end - blocks_file.source_duration_s) > 1e-3:
        raise ValueError("reaction blocks must cover the complete source duration")
    return blocks_file.model_copy(update={"cut_points": ordered_cuts, "blocks": ordered_blocks})


def validate_remix_plan(plan: RemixPlan, blocks_file: ReactionBlocks) -> RemixPlan:
    if plan.source_hash != blocks_file.source_hash:
        raise ValueError("remix plan source hash does not match reaction blocks")
    if abs(plan.original_duration_s - blocks_file.source_duration_s) > 1e-3:
        raise ValueError("remix plan original duration does not match reaction blocks")

    ordered_items = sorted(plan.items, key=lambda item: item.order)
    item_ids = [item.item_id for item in ordered_items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("remix plan item IDs must be unique")
    if [item.order for item in ordered_items] != list(range(len(ordered_items))):
        raise ValueError("remix plan item order must be contiguous from zero")
    block_by_id = {block.block_id: block for block in blocks_file.blocks}
    cut_ids = {cut.cut_point_id for cut in blocks_file.cut_points}
    selected_blocks: list[str] = []
    slot_ids: set[str] = set()
    calculated_duration = 0.0
    for item in ordered_items:
        if item.kind == "source_block":
            if item.block_id not in block_by_id:
                raise ValueError(f"plan item {item.item_id} references unknown block")
            if item.block_id in selected_blocks:
                raise ValueError(f"reaction-remix.v1 forbids block reuse: {item.block_id}")
            block = block_by_id[item.block_id]
            if item.start_cut_point_id not in cut_ids or item.end_cut_point_id not in cut_ids:
                raise ValueError(f"plan item {item.item_id} references invented cut point")
            if item.start_cut_point_id != block.start_cut_point_id or item.end_cut_point_id != block.end_cut_point_id:
                raise ValueError(f"plan item {item.item_id} must use the block safe cut points")
            if block.kind == "commentary":
                raise ValueError(f"plan item {item.item_id} cannot retain commentary as a source block")
            selected_blocks.append(item.block_id)
            calculated_duration += block.tc_end - block.tc_start
        else:
            if item.slot_id in slot_ids:
                raise ValueError(f"duplicate commentary slot ID: {item.slot_id}")
            slot_ids.add(item.slot_id or "")
            if len(item.preferred_visual_block_ids) != 1:
                raise ValueError(f"plan item {item.item_id} must reference exactly one commentary visual block")
            for block_id in item.evidence_block_ids + item.preferred_visual_block_ids:
                if block_id not in block_by_id:
                    raise ValueError(f"plan item {item.item_id} references invented block ID {block_id}")
            visual_block = block_by_id[item.preferred_visual_block_ids[0]]
            if visual_block.kind != "commentary" or not visual_block.eligible_commentary_visual:
                raise ValueError(
                    f"plan item {item.item_id} preferred visual must be an eligible commentary block"
                )
            calculated_duration += item.target_duration_s or 0.0
            expected_budget = round((item.target_duration_s or 0.0) * 6.5)
            if item.char_budget != expected_budget:
                raise ValueError(f"plan item {item.item_id} char_budget must equal round(target_duration_s * 6.5)")

    excluded_ids = [item.block_id for item in plan.excluded_blocks]
    if len(excluded_ids) != len(set(excluded_ids)):
        raise ValueError("excluded block IDs must be unique")
    unknown_excluded = sorted(set(excluded_ids) - set(block_by_id))
    if unknown_excluded:
        raise ValueError(f"excluded blocks contain invented IDs: {unknown_excluded}")
    if set(selected_blocks) & set(excluded_ids):
        raise ValueError("a block cannot be both selected and excluded")
    accounted = set(selected_blocks) | set(excluded_ids)
    if accounted != set(block_by_id):
        raise ValueError("every source block must be selected or listed in excluded_blocks")
    manual_drop_ids = {item.block_id for item in plan.excluded_blocks if item.category == "manual_drop"}
    for block_id in manual_drop_ids:
        if block_by_id[block_id].kind == "commentary":
            raise ValueError("manual_drop is only valid for non-commentary source blocks")
    protected = {block.block_id for block in blocks_file.blocks if block.kind != "commentary"}
    unretained_protected = protected - set(selected_blocks) - manual_drop_ids
    if unretained_protected:
        raise ValueError("all non-commentary blocks must be retained unless explicitly manual_drop")
    commentary_block_ids = {block.block_id for block in blocks_file.blocks if block.kind == "commentary"}
    assigned_commentary_list = [
        item.preferred_visual_block_ids[0]
        for item in ordered_items
        if item.kind == "commentary_slot"
    ]
    if len(assigned_commentary_list) != len(set(assigned_commentary_list)):
        raise ValueError("commentary visual blocks cannot be reused across slots")
    assigned_commentary_ids = set(assigned_commentary_list)
    if assigned_commentary_ids != commentary_block_ids:
        raise ValueError("every commentary block must be assigned to exactly one commentary slot")

    annotation_ids = [item.block_id for item in plan.semantic_annotations]
    if len(annotation_ids) != len(set(annotation_ids)):
        raise ValueError("semantic annotation block IDs must be unique")
    if set(annotation_ids) - set(block_by_id):
        raise ValueError("semantic annotations contain invented block IDs")
    if abs(calculated_duration - plan.predicted_duration_s) > 1e-3:
        raise ValueError("predicted duration does not equal selected spans plus commentary targets")
    calculated_ratio = plan.predicted_duration_s / plan.original_duration_s
    if abs(calculated_ratio - plan.predicted_output_ratio) > 1e-4:
        raise ValueError("predicted output ratio does not match predicted duration")
    ratio_epsilon = 1e-6
    if (
        calculated_ratio < plan.duration_policy.hard_min_output_ratio - ratio_epsilon
        or calculated_ratio > plan.duration_policy.hard_max_output_ratio + ratio_epsilon
    ):
        raise ValueError("predicted output ratio violates hard duration range")
    if plan.retention.unique_reaction_speech_ratio < 0.90:
        raise ValueError("unique reaction speech retention must be at least 0.90")
    return plan.model_copy(update={"items": ordered_items})


def validate_commentary_script(script: CommentaryScript, plan: RemixPlan) -> CommentaryScript:
    if script.source_hash != plan.source_hash:
        raise ValueError("commentary script source hash does not match remix plan")
    plan_slots = {item.slot_id: item for item in plan.items if item.kind == "commentary_slot"}
    script_slots = {slot.slot_id: slot for slot in script.slots}
    if len(script_slots) != len(script.slots):
        raise ValueError("commentary script slot IDs must be unique")
    if set(script_slots) != set(plan_slots):
        raise ValueError("commentary script slots must exactly match plan slots")
    ordered_plan = sorted(plan.items, key=lambda item: item.order)
    for slot_id, slot in script_slots.items():
        item = plan_slots[slot_id]
        assert item is not None
        if not set(slot.evidence_block_ids).issubset(set(item.evidence_block_ids)) or not slot.evidence_block_ids:
            raise ValueError(f"commentary slot {slot_id} evidence must be a non-empty subset of plan evidence")
        if abs(slot.target_duration_s - (item.target_duration_s or 0.0)) > 1e-6:
            raise ValueError(f"commentary slot {slot_id} target duration does not match plan")
        if abs(slot.max_duration_s - (item.max_duration_s or 0.0)) > 1e-6 or slot.char_budget != item.char_budget:
            raise ValueError(f"commentary slot {slot_id} budget does not match plan")
        plan_index = ordered_plan.index(item)
        expected_before = ordered_plan[plan_index - 1].item_id if plan_index > 0 else None
        expected_after = ordered_plan[plan_index + 1].item_id if plan_index + 1 < len(ordered_plan) else None
        if slot.before_item_id != expected_before or slot.after_item_id != expected_after:
            raise ValueError(f"commentary slot {slot_id} adjacency does not match plan order")
        if not all((slot.qa.language_ok, slot.qa.evidence_ok, slot.qa.style_ok, slot.qa.length_ok)):
            raise ValueError(f"commentary slot {slot_id} failed deterministic QA")
    return script


def validate_commentary_audio(audio: CommentaryAudio, script: CommentaryScript) -> CommentaryAudio:
    if audio.source_hash != script.source_hash:
        raise ValueError("commentary audio source hash does not match script")
    item_by_slot = {item.slot_id: item for item in audio.items}
    if len(item_by_slot) != len(audio.items):
        raise ValueError("commentary audio slot IDs must be unique")
    if set(item_by_slot) != {slot.slot_id for slot in script.slots}:
        raise ValueError("commentary audio must contain exactly one item for each script slot")
    measured_total = sum(item.duration_s for item in audio.items)
    if abs(measured_total - audio.total_commentary_duration_s) > 1e-3:
        raise ValueError("total_commentary_duration_s does not equal measured item durations")
    return audio


def validate_audio_assets(assets: AudioAssets, source: ReactionSource | None = None) -> AudioAssets:
    if source is not None and assets.source_hash != source.input_hash:
        raise ValueError("audio assets source hash does not match reaction source")
    asset_ids = [asset.asset_id for asset in assets.items]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("audio asset IDs must be unique")
    for asset in assets.items:
        if asset.source_hash != assets.source_hash:
            raise ValueError(f"audio asset {asset.asset_id} source hash mismatch")
    return assets


def validate_remix_edl(
    edl: RemixEdl,
    source: ReactionSource | None = None,
    commentary_audio: CommentaryAudio | None = None,
) -> RemixEdl:
    if source is not None:
        if edl.source_hash != source.input_hash:
            raise ValueError("remix EDL source hash does not match reaction source")
        if (
            edl.output.width != source.video.width
            or edl.output.height != source.video.height
            or edl.output.fps_num != source.video.fps_num
            or edl.output.fps_den != source.video.fps_den
            or edl.output.audio_sample_rate != source.audio.sample_rate
            or edl.output.audio_channels != source.audio.channels
        ):
            raise ValueError("remix EDL output format must preserve the reaction source format")
    ordered = sorted(edl.placements, key=lambda item: (item.tl_start, item.tl_end, item.placement_id))
    placement_ids = [item.placement_id for item in ordered]
    if len(placement_ids) != len(set(placement_ids)):
        raise ValueError("remix placement IDs must be unique")
    previous_end = 0.0
    valid_tts_paths = {item.audio_path for item in commentary_audio.items} if commentary_audio else None
    for placement in ordered:
        if abs(placement.tl_start - previous_end) > 1e-3:
            raise ValueError(f"remix EDL has a gap or overlap before {placement.placement_id}")
        if valid_tts_paths is not None and placement.audio.mode in {"tts", "tts_bed"}:
            if placement.audio.tts_audio_path not in valid_tts_paths:
                raise ValueError(f"placement {placement.placement_id} references unknown TTS asset")
        previous_end = placement.tl_end
    if not ordered and edl.total_duration_s == 0 and edl.warnings:
        return edl
    if not ordered or abs(previous_end - edl.total_duration_s) > 1e-3:
        raise ValueError("remix EDL final placement must match total duration")
    return edl.model_copy(update={"placements": ordered})


def validate_remix_render_timeline(
    timeline: RemixRenderTimeline,
    edl: RemixEdl | None = None,
) -> RemixRenderTimeline:
    if edl is not None:
        if timeline.source_hash != edl.source_hash:
            raise ValueError("render timeline source hash does not match remix EDL")
        if len(timeline.placements) != len(edl.placements):
            raise ValueError("render timeline placement count does not match remix EDL")
    frame_end = 0
    sample_end = 0
    placement_ids: set[str] = set()
    for placement in timeline.placements:
        if placement.placement_id in placement_ids:
            raise ValueError("render timeline placement IDs must be unique")
        placement_ids.add(placement.placement_id)
        if placement.tl_start_frame != frame_end or placement.tl_start_sample != sample_end:
            raise ValueError(f"render timeline has a gap or overlap before {placement.placement_id}")
        if placement.tl_end_frame <= placement.tl_start_frame or placement.tl_end_sample <= placement.tl_start_sample:
            raise ValueError("render timeline placements must have positive frame and sample duration")
        frame_end = placement.tl_end_frame
        sample_end = placement.tl_end_sample
    if frame_end != timeline.total_frames or sample_end != timeline.total_samples:
        raise ValueError("render timeline totals do not match final placement")
    return timeline


def validate_remix_command_manifest(manifest: RemixCommandManifest) -> RemixCommandManifest:
    command_ids = [command.command_id for command in manifest.commands]
    if len(command_ids) != len(set(command_ids)):
        raise ValueError("render command IDs must be unique")
    lowered_denylist = [term.lower() for term in manifest.denylist]
    for command in manifest.commands:
        command_text = " ".join(command.args).lower()
        forbidden = [term for term in lowered_denylist if term and term in command_text]
        if forbidden:
            raise ValueError(f"render command {command.command_id} contains forbidden filter(s): {forbidden}")
    return manifest


def validate_remix_qa(qa: RemixQa) -> RemixQa:
    hard_fail = any(
        status == "fail"
        for status in (
            qa.duration.status,
            qa.reaction_preservation.status,
            qa.commentary.status,
            qa.visual_policy.status,
            qa.audio.status,
            qa.timeline.status,
        )
    )
    if qa.duration.output_ratio < qa.duration.hard_min_ratio:
        hard_fail = True
    if qa.reaction_preservation.speed_mismatches or qa.reaction_preservation.gain_mismatches or qa.reaction_preservation.span_mismatches:
        hard_fail = True
    if qa.commentary.provider_mismatches or qa.commentary.voice_mismatches or qa.commentary.old_narrator_leakage_count:
        hard_fail = True
    if any((
        qa.visual_policy.mask_operations,
        qa.visual_policy.subtitle_additions,
        qa.visual_policy.text_overlays,
        qa.visual_policy.blur_operations,
        qa.visual_policy.other_overlays,
        qa.timeline.gap_count,
        qa.timeline.overlap_count,
    )) or not qa.timeline.decode_ok:
        hard_fail = True
    expected = "fail" if hard_fail else "pass"
    if qa.status != expected:
        raise ValueError(f"remix QA overall status must be {expected}")
    return qa
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



def validate_story_map(sections: list[StorySection], duration: float | None = None) -> list[StorySection]:
    ordered = sorted(sections, key=lambda item: (item.tc_start, item.tc_end, item.section_id))
    previous_end = -1.0
    for expected_id, section in enumerate(ordered):
        if section.section_id != expected_id:
            raise ValueError(f"section_id must be continuous: expected {expected_id}, got {section.section_id}")
        if section.tc_start < previous_end - 1e-3:
            raise ValueError(f"story section #{section.section_id} overlaps previous section")
        if duration is not None and section.tc_end > duration + 1e-6:
            raise ValueError(f"story section #{section.section_id} exceeds duration")
        previous_end = section.tc_end
    return ordered

def validate_review_intents(intents: list[ReviewIntent], beats: list[ReviewBeat]) -> list[ReviewIntent]:
    ordered = sorted(intents, key=lambda item: item.beat_id)
    beat_ids = {beat.beat_id for beat in beats}
    if {item.beat_id for item in ordered} != beat_ids:
        raise ValueError("review intent beat ids must match review_script beat ids")
    return ordered

def validate_shot_visual_index(index: ShotVisualIndexFile, shots: list[Shot] | None = None) -> ShotVisualIndexFile:
    ordered = sorted(index.shots, key=lambda item: item.shot_index)
    if index.meta.n_shots != len(ordered):
        raise ValueError("visual index meta n_shots must match shots length")
    if len({item.shot_index for item in ordered}) != len(ordered):
        raise ValueError("visual index shot ids must be unique")
    if shots is not None:
        by_index = {item.shot_index: item for item in ordered}
        missing = sorted(shot.index for shot in shots if shot.index not in by_index)
        if missing:
            raise ValueError(f"visual index is missing candidate shot ids: {missing[:10]}")
        for shot in shots:
            item = by_index[shot.index]
            if abs(item.tc_start - shot.tc_start) > 1e-3 or abs(item.tc_end - shot.tc_end) > 1e-3:
                raise ValueError(f"visual index shot #{shot.index} timecode does not match shots.json")
            if abs(item.duration - shot.duration) > 1e-3:
                raise ValueError(f"visual index shot #{shot.index} duration does not match shots.json")
    return index.model_copy(update={"shots": ordered})

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
