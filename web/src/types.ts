export type RunKind = "single" | "series";
export type JobStatus =
  | "queued"
  | "starting"
  | "running"
  | "cancel_requested"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "orphaned"
  | "blocked";
export type StageStatus = "pending" | "running" | "succeeded" | "skipped_valid" | "warning" | "failed" | "cancelled" | "blocked";
export type QaStatus = "pass" | "warn" | "block" | "unknown";

export interface RuntimeCheck {
  key: string;
  label: string;
  status: QaStatus;
  detail?: string;
}

export interface HealthResponse {
  status: QaStatus;
  version?: string;
  active_job_id?: string | null;
  queue_depth?: number;
  checks?: RuntimeCheck[];
  providers?: Record<string, boolean>;
}

export interface Preset {
  id: string;
  name: string;
  path: string;
  token: string;
  kind?: RunKind | "both";
  description?: string;
}

export interface FsRoot {
  id: string;
  label: string;
  token: string;
  path?: string;
}

export interface FsEntry {
  token: string;
  name: string;
  kind: "file" | "directory";
  extension?: string;
  size?: number;
  modified_at?: string;
}

export interface StageState {
  key: string;
  label?: string;
  episode_key?: string | null;
  status: StageStatus;
  attempt?: number;
  started_at?: string | null;
  finished_at?: string | null;
  message?: string | null;
}

export interface Job {
  id: string;
  run_id?: string;
  kind: RunKind;
  title?: string;
  run_dir: string;
  status: JobStatus;
  current_stage?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  last_heartbeat?: string | null;
  exit_code?: number | null;
  queue_position?: number | null;
  delivery_status?: QaStatus;
  stages?: StageState[];
  error?: string | null;
}

export interface RunSummary {
  id: string;
  job_id?: string;
  kind: RunKind;
  title: string;
  run_dir: string;
  status: JobStatus;
  delivery_status?: QaStatus;
  episode_count?: number;
  current_stage?: string | null;
  updated_at?: string;
  output_artifact_id?: string | null;
}

export interface PlanStage {
  key: string;
  label: string;
  episode_key?: string;
  status?: StageStatus;
}

export interface PlanResponse {
  plan_id: string;
  kind: RunKind;
  command: string | string[];
  run_dir: string;
  stages: PlanStage[];
  warnings: string[];
  can_start: boolean;
  dry_run_output?: string;
  expires_at?: string;
}

export interface PreflightResponse {
  can_start: boolean;
  checks: RuntimeCheck[];
  warnings?: string[];
}

export interface JobEvent {
  id: number;
  type: "job_state" | "stage_state" | "log" | "artifact" | "qa" | "heartbeat";
  timestamp: string;
  level?: "debug" | "info" | "warning" | "error";
  stage?: string | null;
  message?: string;
  payload?: Record<string, unknown>;
}

export interface EpisodeSummary {
  key: string;
  number?: number;
  title?: string;
  status: QaStatus;
  stages: StageState[];
  duration_s?: number;
  translation_ratio?: number;
  approximate_timecodes?: boolean;
}

export interface Artifact {
  id: string;
  name: string;
  kind: "json" | "html" | "image" | "audio" | "video" | "log" | "text" | "other";
  stage?: string;
  episode_key?: string;
  size?: number;
  modified_at?: string;
  preview?: unknown;
  relative_path?: string;
  media_type?: string;
}

export interface QaMetric {
  key: string;
  label: string;
  status: QaStatus;
  value?: string | number | boolean | null;
  detail?: string;
  artifact_id?: string;
}

export interface Chapter {
  index: number;
  episode_key?: string;
  title: string;
  start_s: number;
  duration_s?: number;
  status?: QaStatus;
}

export interface EdlPlacement {
  id: string;
  episode_key?: string;
  beat_id?: number;
  tl_start: number;
  tl_end: number;
  src?: string;
  src_in?: number;
  src_out?: number;
  thumbnail_artifact_id?: string;
  status?: QaStatus;
  warnings?: string[];
}

export interface RunQa {
  status: QaStatus;
  metrics: QaMetric[];
  warnings?: string[];
  blocker_codes?: string[];
  chapters?: Chapter[];
  placements?: EdlPlacement[];
  durations?: {
    composer_estimate_s?: number;
    tts_s?: number;
    edl_s?: number;
    render_s?: number;
    minimum_s?: number;
    target_max_s?: number;
    hard_cap_s?: number;
  };
  output_artifact_id?: string;
}

export interface EpisodeDetail extends EpisodeSummary {
  film_map?: unknown[];
  review_beats?: unknown[];
  timings?: unknown[];
  shots?: unknown[];
}

export interface ListResponse<T> {
  items: T[];
}
