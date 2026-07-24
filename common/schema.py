from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SegmentType = Literal["speech", "visual"]
ProviderMode = Literal["auto", "ai33", "genmax", "openai"]
AsrProvider = Literal["faster-whisper", "openai-gpt4o", "openai-gpt4o-hybrid", "manual"]
AlignerProvider = Literal["none", "whisperx", "qwen3"]
TimecodeQuality = Literal["strict", "approximate"]
TranscriptCorrectionMode = Literal["off", "glossary", "openai"]
SourceLanguage = Literal["ko", "vi", "ja"]
TranslateMode = Literal["ko-en", "ja-en", "none"]
AnimeContentType = Literal["anime_series", "anime_movie"]
ContentType = Literal["episode", "movie", "anime_series", "anime_movie"]
RequestedRecapMode = Literal["off", "auto", "full", "quick", "merge", "skip"]
ResolvedRecapMode = Literal["full", "quick", "merge", "skip"]
SeriesRecapFormat = Literal["compact", "episode_chaptered", "episode_arc_chaptered"]
SeriesRecapDetailLevel = Literal["standard", "detailed"]
AnimeNonStoryLabel = Literal[
    "opening_theme",
    "ending_theme",
    "next_episode_preview",
    "eyecatch",
    "recap_previous_episode",
    "sponsor_card",
    "title_card",
    "studio_logo",
]
ANIME_NON_STORY_LABELS = {
    "opening_theme",
    "ending_theme",
    "next_episode_preview",
    "eyecatch",
    "recap_previous_episode",
    "sponsor_card",
    "title_card",
    "studio_logo",
}

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
    vision_provider: str = "openai"
    gap_threshold: float = Field(ge=0)
    max_vision_frames: int = Field(ge=0)
    max_visual_gap_s: float = Field(ge=0, default=20.0)
    translation_required: bool = False
    translation_min_success_ratio: float | None = Field(default=None, ge=0, le=1)
    translation_total_count: int = Field(default=0, ge=0)
    translation_success_count: int = Field(default=0, ge=0)
    translation_success_ratio: float | None = Field(default=None, ge=0, le=1)
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
    context_file_path: str | None = None
    context_file_hash: str | None = None
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
    review_script_hash: str | None = None



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

class AnimeNonStoryRange(NonStoryRange):
    label: AnimeNonStoryLabel
    confidence: float = Field(default=1.0, ge=0, le=1)

class AnimeCharacter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_vi: str
    name_original: str | None = None
    aliases: list[str] = Field(default_factory=list)
    role: str | None = None
    pronunciation: str | None = None

    @field_validator("name_vi", "name_original", "role", "pronunciation")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_character(self) -> "AnimeCharacter":
        if not self.name_vi:
            raise ValueError("character name_vi cannot be empty")
        return self

class AnimeTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    meaning_vi: str | None = None
    aliases: list[str] = Field(default_factory=list)
    pronunciation: str | None = None

    @field_validator("term", "meaning_vi", "pronunciation")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_term(self) -> "AnimeTerm":
        if not self.term:
            raise ValueError("anime term cannot be empty")
        return self

class AnimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    kind: AnimeContentType
    season: int | str | None = None
    episode_number: int | str | None = None
    episode_title: str | None = None
    movie_year: int | None = Field(default=None, ge=1900)
    arc: str | None = None
    continuity_notes: str | None = None
    characters: list[AnimeCharacter] = Field(default_factory=list)
    terms: list[AnimeTerm] = Field(default_factory=list)
    pronunciation_hints: list[str] = Field(default_factory=list)
    non_story_ranges: list[AnimeNonStoryRange] = Field(default_factory=list)

    @field_validator("title", "episode_title", "arc", "continuity_notes")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("pronunciation_hints")
    @classmethod
    def normalize_pronunciation_hints(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_context(self) -> "AnimeContext":
        if not self.title:
            raise ValueError("anime context title cannot be empty")
        if self.kind == "anime_series" and self.episode_number is None:
            raise ValueError("anime_series context requires episode_number")
        return self

class SeriesManifestEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_key: str | None = None
    episode_number: int | str | None = None
    title: str | None = None
    source_path: str | None = None
    arc: str | None = None
    spoiler_limit_episode: int | str | None = None

    @field_validator("episode_key", "title", "source_path", "arc")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

class SeriesManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    series_title: str | None = None
    season: int | str | None = None
    episode_key: str | None = None
    episode_number: int | str | None = None
    title: str | None = None
    source_path: str | None = None
    arc: str | None = None
    spoiler_limit_episode: int | str | None = None
    episodes: list[SeriesManifestEpisode] = Field(default_factory=list)

    @field_validator("series_id", "series_title", "episode_key", "title", "source_path", "arc")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_manifest(self) -> "SeriesManifest":
        if not self.series_id:
            raise ValueError("series_id cannot be empty")
        if not self.episodes and not any((self.episode_key, self.episode_number, self.source_path)):
            raise ValueError("series_manifest requires episodes or current episode fields")
        return self

class EpisodeScoreSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reveal: float = Field(ge=0, le=1)
    state_change: float = Field(ge=0, le=1)
    fight_action: float = Field(ge=0, le=1)
    new_entity: float = Field(ge=0, le=1)
    continuity_dependency: float = Field(ge=0, le=1)
    story_density: float = Field(ge=0, le=1)
    non_story_ratio: float = Field(ge=0, le=1)
    non_story_penalty: float = Field(ge=0, le=1)

class EpisodeTimecodeHook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    label: str
    summary: str

    @field_validator("label", "summary")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("episode timecode hook text cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_hook(self) -> "EpisodeTimecodeHook":
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

class EpisodeMemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    episode_key: str
    episode_number: int | str | None = None
    title: str | None = None
    source_path: str | None = None
    arc: str | None = None
    recap_mode: ResolvedRecapMode
    importance_score: float = Field(ge=0, le=1)
    summary: str
    entity_hooks: list[str] = Field(default_factory=list)
    arc_hooks: list[str] = Field(default_factory=list)
    important_timecodes: list[EpisodeTimecodeHook] = Field(default_factory=list)
    created_at: datetime

    @field_validator("series_id", "episode_key", "title", "source_path", "arc", "summary")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized and value is not None:
            raise ValueError("episode memory text field cannot be empty")
        return normalized

    @field_validator("entity_hooks", "arc_hooks")
    @classmethod
    def normalize_lists(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

class EpisodeMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["episode_memory"] = "episode_memory"
    anime_context: AnimeContext | None = None
    current: EpisodeMemoryEntry
    previous: list[EpisodeMemoryEntry] = Field(default_factory=list)
    spoiler_limit_episode: int | str | None = None
    review_guidance: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime

    @field_validator("review_guidance", "warnings")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

class EpisodeMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    episode_key: str
    episode_number: int | str | None = None
    title: str | None = None
    source_path: str
    arc: str | None = None
    spoiler_limit_episode: int | str | None = None
    requested_recap_mode: RequestedRecapMode
    recap_mode: ResolvedRecapMode
    importance_score: float = Field(ge=0, le=1)
    score_signals: EpisodeScoreSignals
    score_reasons: list[str] = Field(default_factory=list)
    short_circuit: bool
    target_ratio_override: float | None = Field(default=None, ge=0)
    quick_target_ratio: float | None = Field(default=None, ge=0)
    thresholds: dict[str, float] = Field(default_factory=dict)
    previous_memory_count: int = Field(ge=0)
    memory_index_path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    film_map_hash: str | None = None
    story_map_hash: str | None = None
    video_profile_hash: str | None = None
    anime_context_hash: str | None = None
    series_manifest_hash: str | None = None
    source_hash: str | None = None
    config_hash: str | None = None
    cache_version: str | None = None

    @field_validator("series_id", "episode_key", "title", "source_path", "arc", "memory_index_path")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized and value is not None:
            raise ValueError("episode meta text field cannot be empty")
        return normalized

    @field_validator("score_reasons", "warnings")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))


class SeriesSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    episode_key: str
    src: str
    source_path: str
    from_seg_id: int = Field(ge=0)
    to_seg_id: int = Field(ge=0)
    src_tc_start: float = Field(ge=0)
    src_tc_end: float = Field(gt=0)

    @field_validator("event_id", "episode_key", "src", "source_path")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("series source ref text field cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_ref(self) -> "SeriesSourceRef":
        if self.to_seg_id < self.from_seg_id:
            raise ValueError("series source ref to_seg_id must be >= from_seg_id")
        if self.src_tc_end <= self.src_tc_start:
            raise ValueError("series source ref src_tc_end must be greater than src_tc_start")
        return self


class SeriesEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    series_id: str
    episode_key: str
    episode_number: int | str | None = None
    title: str | None = None
    source_path: str
    arc: str | None = None
    recap_mode: ResolvedRecapMode
    summary: str
    event_type: str = "story_section"
    from_seg_id: int = Field(ge=0)
    to_seg_id: int = Field(ge=0)
    tc_start: float = Field(ge=0)
    tc_end: float = Field(gt=0)
    importance: float = Field(default=0.5, ge=0, le=1)
    is_hook_candidate: bool = False
    entity_hooks: list[str] = Field(default_factory=list)
    arc_hooks: list[str] = Field(default_factory=list)

    @field_validator("event_id", "series_id", "episode_key", "title", "source_path", "arc", "summary", "event_type")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("series event text field cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_event(self) -> "SeriesEvent":
        if self.to_seg_id < self.from_seg_id:
            raise ValueError("series event to_seg_id must be >= from_seg_id")
        if self.tc_end <= self.tc_start:
            raise ValueError("series event tc_end must be greater than tc_start")
        return self

    @field_validator("entity_hooks", "arc_hooks")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))


class EpisodeTargetPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_key: str
    episode_number: int | str | None = None
    title: str | None = None
    arc: str | None = None
    recap_mode: ResolvedRecapMode
    source_duration_s: float = Field(ge=0)
    story_duration_s: float = Field(ge=0)
    importance_score: float = Field(default=0.0, ge=0, le=1)
    continuity_dependency: float = Field(default=0.0, ge=0, le=1)
    event_count: int = Field(default=0, ge=0)
    target_video_s: float = Field(ge=0)
    char_budget: int = Field(ge=0)
    min_chars: int = Field(ge=0)
    target_beats: int = Field(ge=0)

    @field_validator("episode_key", "title", "arc")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("episode target text field cannot be empty")
        return normalized

class SeriesArcPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arc_id: str
    title: str
    episode_keys: list[str] = Field(default_factory=list)
    target_video_s: float = Field(ge=0)
    char_budget: int = Field(ge=0)
    min_chars: int = Field(ge=0)
    target_beats: int = Field(ge=0)
    episodes: list[EpisodeTargetPlan] = Field(default_factory=list)

    @field_validator("arc_id", "title")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("series arc plan text field cannot be empty")
        return normalized

    @field_validator("episode_keys")
    @classmethod
    def normalize_episode_keys(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

class SeasonTargetPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recap_format: SeriesRecapFormat
    detail_level: SeriesRecapDetailLevel = "standard"
    target_total_min_s: float = Field(ge=0)
    target_total_max_s: float = Field(ge=0)
    target_total_hard_cap_s: float = Field(ge=0)
    episode_min_s: float = Field(ge=0)
    episode_normal_s: float = Field(ge=0)
    episode_high_s: float = Field(ge=0)
    arc_size: int = Field(ge=1)
    total_target_video_s: float = Field(ge=0)
    total_char_budget: int = Field(ge=0)
    min_total_chars: int = Field(ge=0)
    max_total_chars: int = Field(ge=0)
    episode_count: int = Field(ge=0)
    arc_count: int = Field(ge=0)
    arcs: list[SeriesArcPlan] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds(self) -> "SeasonTargetPlan":
        if self.target_total_max_s and self.target_total_max_s < self.target_total_min_s:
            raise ValueError("target_total_max_s must be >= target_total_min_s")
        if self.target_total_hard_cap_s and self.target_total_max_s and self.target_total_hard_cap_s < self.target_total_max_s:
            raise ValueError("target_total_hard_cap_s must be >= target_total_max_s")
        if self.arc_count != len(self.arcs):
            raise ValueError("arc_count must match arcs length")
        return self

class SeriesEventBank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    series_title: str | None = None
    recap_format: SeriesRecapFormat = "compact"
    episode_keys: list[str] = Field(default_factory=list)
    target_video_s: float = Field(gt=0)
    char_budget: int = Field(gt=0)
    episode_targets: list[EpisodeTargetPlan] = Field(default_factory=list)
    season_target_plan: SeasonTargetPlan | None = None
    events: list[SeriesEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime

    @field_validator("series_id", "series_title")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("series event bank text field cannot be empty")
        return normalized


class SeriesReviewBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    narration: str
    source_refs: list[SeriesSourceRef] = Field(default_factory=list)
    is_hook: bool = False

    @field_validator("narration")
    @classmethod
    def validate_narration(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("series narration cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_beat(self) -> "SeriesReviewBeat":
        if not self.source_refs:
            raise ValueError("series review beat requires at least one source_ref")
        return self


class SeriesReviewMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    target_video_s: float = Field(gt=0)
    char_budget: int = Field(gt=0)
    est_total_chars: int = Field(ge=0)
    n_events: int = Field(ge=0)
    selected_event_ids: list[str] = Field(default_factory=list)
    qa_report: list[dict[str, Any]] = Field(default_factory=list)
    model_versions: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime


class SeriesComposerQa(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str
    recap_format: SeriesRecapFormat
    detail_level: SeriesRecapDetailLevel = "standard"
    target_video_s: float = Field(ge=0)
    target_total_hard_cap_s: float | None = Field(default=None, ge=0)
    char_budget: int = Field(ge=0)
    est_total_chars: int = Field(ge=0)
    estimated_duration_s: float = Field(ge=0)
    n_events: int = Field(ge=0)
    selected_event_ids: list[str] = Field(default_factory=list)
    qa_report: list[dict[str, Any]] = Field(default_factory=list)
    revision_count: int = Field(ge=0)
    prompt_count: int = Field(ge=0)
    arc_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime

class SeriesChapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    start_beat_id: int = Field(ge=0)
    episode_key: str | None = None

    @field_validator("title", "episode_key")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("series chapter text field cannot be empty")
        return normalized

class EdlSourceMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    sources: dict[str, str]
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_sources(self) -> "EdlSourceMap":
        if not self.sources:
            raise ValueError("edl source map requires at least one source")
        for key, value in self.sources.items():
            if not key.strip() or not value.strip():
                raise ValueError("edl source map keys and paths cannot be empty")
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
    source_count: int | None = Field(default=None, ge=0)
    source_names: list[str] = Field(default_factory=list)
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


def validate_series_review_script(beats: list[SeriesReviewBeat]) -> list[SeriesReviewBeat]:
    ordered = sorted(beats, key=lambda item: item.beat_id)
    for expected_id, beat in enumerate(ordered):
        if beat.beat_id != expected_id:
            raise ValueError(f"series beat_id must be continuous: expected {expected_id}, got {beat.beat_id}")
    if ordered and not ordered[0].is_hook:
        raise ValueError("first series beat must be a hook")
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
