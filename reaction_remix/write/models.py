from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WrittenSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    text_ja: str

    @field_validator("text_ja")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text_ja cannot be empty")
        return text


class WrittenSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: list[WrittenSlot]

    @model_validator(mode="after")
    def validate_unique_slots(self) -> "WrittenSlots":
        ids = [slot.slot_id for slot in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("written commentary slot IDs must be unique")
        return self


class ScriptQaIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    issue_type: str
    suggestion: str


class ScriptQaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(alias="pass")
    issues: list[ScriptQaIssue] = Field(default_factory=list)
    notes: str = ""
