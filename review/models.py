from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OutlineBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_seg_id: int = Field(ge=0)
    to_seg_id: int = Field(ge=0)
    summary: str
    is_hook: bool = False

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("summary cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_span(self) -> "OutlineBeat":
        if self.to_seg_id < self.from_seg_id:
            raise ValueError("to_seg_id must be >= from_seg_id")
        return self


class OutlineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    glossary: list[dict[str, Any]] = Field(default_factory=list)
    outline: list[OutlineBeat]
    hook: list[int] = Field(default_factory=list)


class NarrationBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    narration: str

    @field_validator("narration")
    @classmethod
    def validate_narration(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("narration cannot be empty")
        if not any(char.isalnum() for char in normalized):
            raise ValueError("narration cannot be a placeholder")
        return normalized


class QaIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: int = Field(ge=0)
    type: str
    suggestion: str


class QaResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(alias="pass")
    issues: list[QaIssue] = Field(default_factory=list)
    notes: str = ""

    def model_dump_public(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)
