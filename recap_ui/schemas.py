from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobKind(StrEnum):
    SINGLE = "single"
    SERIES = "series"


class JobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"
    BLOCKED = "blocked"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED_VALID = "skipped_valid"
    WARNING = "warning"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class EventType(StrEnum):
    JOB_STATE = "job_state"
    STAGE_STATE = "stage_state"
    LOG = "log"
    ARTIFACT = "artifact"
    QA = "qa"
    HEARTBEAT = "heartbeat"


class DeliveryStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    UNKNOWN = "unknown"


class ArtifactKind(StrEnum):
    JSON = "json"
    HTML = "html"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    LOG = "log"
    TEXT = "text"
    BINARY = "binary"


class FilesystemRoot(ApiModel):
    id: str
    label: str
    token: str


class FilesystemEntry(ApiModel):
    token: str
    name: str
    kind: Literal["file", "directory"]
    suffix: str = ""
    size: int | None = None
    modified_at: datetime | None = None


class PlanCheck(ApiModel):
    code: str
    status: DeliveryStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class PlanDagNode(ApiModel):
    key: str
    label: str
    status: str = "planned"
    outputs: list[str] = Field(default_factory=list)


class SinglePlanRequest(ApiModel):
    source_token: str
    run_dir_token: str | None = None
    run_parent_token: str | None = None
    run_name: str | None = None
    config_token: str
    from_stage: str | None = None
    to_stage: str | None = None
    only: str | None = None
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_run_target(self) -> SinglePlanRequest:
        if self.run_dir_token and (self.run_parent_token or self.run_name):
            raise ValueError("use run_dir_token or run_parent_token + run_name, not both")
        if not self.run_dir_token and not (self.run_parent_token and self.run_name):
            raise ValueError("run_dir_token or run_parent_token + run_name is required")
        return self


class SeriesPlanRequest(ApiModel):
    manifest_token: str
    run_dir_token: str | None = None
    run_parent_token: str | None = None
    run_name: str | None = None
    config_token: str
    episodes: str | None = None
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_run_target(self) -> SeriesPlanRequest:
        if self.run_dir_token and (self.run_parent_token or self.run_name):
            raise ValueError("use run_dir_token or run_parent_token + run_name, not both")
        if not self.run_dir_token and not (self.run_parent_token and self.run_name):
            raise ValueError("run_dir_token or run_parent_token + run_name is required")
        return self


class ExecutionPlan(ApiModel):
    plan_id: str
    kind: JobKind
    command: list[str]
    dry_run_command: list[str]
    run_dir: str
    config_path: str
    config_snapshot: dict[str, Any]
    dag: list[PlanDagNode]
    output_paths: list[str] = Field(default_factory=list)
    checks: list[PlanCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run_output: str = ""
    can_start: bool = True
    command_hash: str
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def argv(self) -> list[str]:
        return list(self.command)


class JobRecord(ApiModel):
    id: str
    plan_id: str
    kind: JobKind
    status: JobStatus
    argv: list[str]
    command_hash: str
    run_dir: str
    config_path: str
    config_snapshot: dict[str, Any]
    parent_job_id: str | None = None
    attempt: int = 1
    worker_id: str | None = None
    pid: int | None = None
    process_create_time: float | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    exit_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def command(self) -> list[str]:
        return list(self.argv)


class JobStageRecord(ApiModel):
    job_id: str
    stage_key: str
    episode_key: str = ""
    attempt: int = 1
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    validator_result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class JobEvent(ApiModel):
    seq: int
    job_id: str
    timestamp: datetime
    event_type: EventType
    level: str = "INFO"
    stage: str | None = None
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ResourceLock(ApiModel):
    resource: str
    job_id: str
    acquired_at: datetime


class RegisteredRunRecord(ApiModel):
    id: str
    path: str
    kind: JobKind | None = None
    created_at: datetime


class ArtifactRecord(ApiModel):
    id: str
    run_id: str
    relative_path: str
    name: str
    kind: ArtifactKind
    size: int
    modified_at: datetime
    media_type: str
    previewable: bool = True


class RunRecord(ApiModel):
    id: str
    kind: JobKind
    name: str
    path_token: str
    job_id: str | None = None
    episode_keys: list[str] = Field(default_factory=list)
    output_artifact_id: str | None = None
    execution_status: JobStatus | None = None
    delivery_status: DeliveryStatus = DeliveryStatus.UNKNOWN
    modified_at: datetime


class EpisodeRecord(ApiModel):
    episode_key: str
    title: str | None = None
    episode_number: int | str | None = None
    stage_statuses: dict[str, StageStatus] = Field(default_factory=dict)
    translation_ratio: float | None = None
    approximate_timecodes: bool | None = None


class EpisodeDetail(ApiModel):
    episode: EpisodeRecord
    film_map: list[dict[str, Any]] = Field(default_factory=list)
    story_map: list[dict[str, Any]] = Field(default_factory=list)
    review_script: list[dict[str, Any]] = Field(default_factory=list)
    beats_timing: list[dict[str, Any]] = Field(default_factory=list)
    shots: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QaCheck(ApiModel):
    code: str
    label: str
    status: DeliveryStatus
    message: str
    artifact: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class QaReport(ApiModel):
    run_id: str
    kind: JobKind
    status: DeliveryStatus
    checks: list[QaCheck]
    metrics: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)


class RuntimeCheck(ApiModel):
    code: str
    status: DeliveryStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeHealth(ApiModel):
    status: DeliveryStatus
    runtime: list[RuntimeCheck]
    providers: dict[str, bool] = Field(default_factory=dict)
    active_job: JobRecord | None = None
    generated_at: datetime = Field(default_factory=utc_now)


class RerunRequest(ApiModel):
    scope: Literal["stage", "final", "all"]
    stage: str | None = None
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> RerunRequest:
        if self.scope == "stage" and not self.stage:
            raise ValueError("stage is required when scope=stage")
        if self.scope == "all" and not self.confirmed:
            raise ValueError("confirmed=true is required when scope=all")
        return self
