from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlannerBlockChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    role: str
    reason: str


class PlannerCommentarySlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    after_block_id: str | None = None
    role: str
    evidence_block_ids: list[str] = Field(min_length=1)
    preferred_visual_block_ids: list[str] = Field(min_length=1, max_length=1)
    reason: str


class PlannerExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    reason_code: str
    reason: str


class PlannerSemanticAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    summary_ja: str | None = None
    country: str | None = None
    topic: str | None = None
    sentiment: str | None = None
    intensity: float | None = Field(default=None, ge=0, le=1)
    novelty: float | None = Field(default=None, ge=0, le=1)


class PlannerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordered_blocks: list[PlannerBlockChoice] = Field(min_length=1)
    commentary_slots: list[PlannerCommentarySlot] = Field(default_factory=list)
    exclusions: list[PlannerExclusion] = Field(default_factory=list)
    semantic_annotations: list[PlannerSemanticAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "PlannerDraft":
        block_ids = [item.block_id for item in self.ordered_blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("ordered_blocks contains duplicate block IDs")
        slot_ids = [item.slot_id for item in self.commentary_slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("commentary_slots contains duplicate slot IDs")
        exclusion_ids = [item.block_id for item in self.exclusions]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise ValueError("exclusions contains duplicate block IDs")
        annotation_ids = [item.block_id for item in self.semantic_annotations]
        if len(annotation_ids) != len(set(annotation_ids)):
            raise ValueError("semantic_annotations contains duplicate block IDs")
        return self
