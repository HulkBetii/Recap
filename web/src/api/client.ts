import type {
  Artifact, Chapter, EdlPlacement, EpisodeDetail, EpisodeSummary, FsEntry, FsRoot, HealthResponse, Job, JobEvent,
  FsListing, PlanResponse, PreflightResponse, Preset, QaMetric, RunQa, RunSummary, StageState,
  SeriesSourceInspection, SingleSourceInspection,
} from "../types";

const API_ROOT = "/api";
let cachedToken = "";

function startupToken(): string {
  if (cachedToken) return cachedToken;
  const metas = document.querySelectorAll<HTMLMetaElement>('meta[name="recap-token"]');
  const meta = metas.item(metas.length - 1)?.content;
  const stored = window.localStorage.getItem("recap.startupToken");
  if (stored) return stored;
  return meta && !meta.startsWith("__") ? meta : "";
}

async function mutationToken(): Promise<string> {
  const existing = startupToken();
  if (existing) return existing;
  const response = await fetch(`${API_ROOT}/session`);
  if (!response.ok) return "";
  const session = await response.json() as { token?: string };
  cachedToken = session.token ?? "";
  return cachedToken;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET") {
    const token = await mutationToken();
    if (token) headers.set("X-Recap-Token", token);
  }
  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: unknown };
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    throw new Error(detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function artifactContent(runId: string, artifactId: string): Promise<unknown> {
  const response = await fetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`);
  if (!response.ok) throw new Error(`Artifact request failed (${response.status})`);
  const type = response.headers.get("content-type") ?? "";
  const text = await response.text();
  if (type.includes("json") || ["[", "{"].some((prefix) => text.trimStart().startsWith(prefix))) {
    try { return JSON.parse(text) as unknown; } catch { return text; }
  }
  return text;
}

interface ApiCheck { code: string; status: "pass" | "warn" | "block" | "unknown"; message: string; label?: string; artifact?: string | null; details?: Record<string, unknown>; }
interface ApiJob { id: string; kind: "single" | "series"; status: Job["status"]; run_dir: string; display_title?: string | null; attempt: number; heartbeat_at?: string | null; exit_code?: number | null; error_message?: string | null; created_at: string; started_at?: string | null; finished_at?: string | null; }
interface ApiStage { stage_key: string; episode_key?: string; attempt?: number; status: StageState["status"]; started_at?: string | null; finished_at?: string | null; error?: string | null; }
interface ApiRun { id: string; job_id?: string | null; kind: "single" | "series"; name: string; display_title?: string | null; path_token: string; episode_keys: string[]; output_artifact_id?: string | null; execution_status?: Job["status"] | null; delivery_status: RunQa["status"]; management_mode?: "managed" | "artifact_only"; available_actions?: string[]; read_only_reason?: string | null; modified_at: string; }
interface ApiArtifact { id: string; relative_path: string; name: string; kind: string; size: number; modified_at: string; media_type: string; }
interface ApiEpisode { episode_key: string; title?: string | null; episode_number?: number | string | null; stage_statuses: Record<string, StageState["status"]>; translation_ratio?: number | null; approximate_timecodes?: boolean | null; }
interface ApiQa { run_id: string; kind: "single" | "series"; status: RunQa["status"]; checks: ApiCheck[]; metrics: Record<string, unknown>; }
interface ApiPlan { plan_id: string; kind: "single" | "series"; command: string[]; run_dir: string; dag: { key: string; label: string; status?: string; outputs?: string[] }[]; checks: ApiCheck[]; output_paths?: string[]; warnings: string[]; can_start: boolean; dry_run_output?: string; created_at?: string; }
interface ApiEvent { seq: number; timestamp: string; event_type: JobEvent["type"]; level?: string; stage?: string | null; message?: string; payload?: Record<string, unknown>; }

function fileName(path: string): string { return path.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? path; }
function mapStage(stage: ApiStage): StageState { return { key: stage.stage_key, episode_key: stage.episode_key || undefined, status: stage.status, attempt: stage.attempt, started_at: stage.started_at, finished_at: stage.finished_at, message: stage.error }; }
function mapJob(job: ApiJob, stages: ApiStage[] = []): Job {
  const mapped = stages.map(mapStage);
  const running = mapped.find((stage) => stage.status === "running");
  return { id: job.id, kind: job.kind, title: job.display_title ?? fileName(job.run_dir), run_dir: job.run_dir, status: job.status, current_stage: running?.key ?? null, created_at: job.created_at, started_at: job.started_at, finished_at: job.finished_at, last_heartbeat: job.heartbeat_at, exit_code: job.exit_code, stages: mapped, error: job.error_message, queue_position: null };
}
function mapRun(run: ApiRun): RunSummary { return { id: run.id, job_id: run.job_id ?? undefined, kind: run.kind, title: run.display_title ?? run.name, display_title: run.display_title ?? run.name, run_dir: run.name, management_mode: run.management_mode ?? (run.job_id ? "managed" : "artifact_only"), available_actions: run.available_actions ?? ["view"], read_only_reason: run.read_only_reason, status: run.execution_status ?? "interrupted", delivery_status: run.delivery_status, episode_count: run.episode_keys.length || undefined, updated_at: run.modified_at, output_artifact_id: run.output_artifact_id }; }
function mapEpisode(episode: ApiEpisode): EpisodeSummary { return { key: episode.episode_key, number: typeof episode.episode_number === "number" ? episode.episode_number : undefined, title: episode.title ?? (episode.episode_number != null ? `Episode ${episode.episode_number}` : episode.episode_key), status: Object.values(episode.stage_statuses).some((status) => ["failed", "blocked"].includes(status)) ? "block" : "pass", stages: Object.entries(episode.stage_statuses).map(([key, status]) => ({ key, status })), translation_ratio: episode.translation_ratio ?? undefined, approximate_timecodes: episode.approximate_timecodes ?? undefined };
}
function mapArtifact(artifact: ApiArtifact): Artifact {
  const episode = artifact.relative_path.split("/")[0];
  const kind = artifact.kind === "binary" ? "other" : artifact.kind as Artifact["kind"];
  return { id: artifact.id, name: artifact.name, kind, stage: artifact.relative_path.split("/").at(-2), episode_key: episode && /^(s\d+e\d+|e\d+)$/i.test(episode) ? episode : undefined, size: artifact.size, modified_at: artifact.modified_at, relative_path: artifact.relative_path, media_type: artifact.media_type };
}
export function normalizeEvent(event: ApiEvent): JobEvent { return { id: event.seq, type: event.event_type, timestamp: event.timestamp, level: event.level?.toLowerCase() as JobEvent["level"], stage: event.stage, message: event.message, payload: event.payload }; }

function numberMetric(metrics: Record<string, unknown>, key: string): number | undefined { const value = metrics[key]; return typeof value === "number" ? value : undefined; }
function firstNumberMetric(metrics: Record<string, unknown>, ...keys: string[]): number | undefined { return keys.map((key) => numberMetric(metrics, key)).find((value) => value !== undefined); }
function qaMetrics(checks: ApiCheck[], artifacts: ApiArtifact[]): QaMetric[] { return checks.map((check) => ({ key: check.code, label: check.label ?? check.code.replaceAll("_", " "), status: check.status, value: check.status.toUpperCase(), detail: check.message, artifact_id: check.artifact ? artifacts.find((item) => item.relative_path === check.artifact || item.relative_path.endsWith(`/${check.artifact}`) || item.name === check.artifact)?.id : undefined })); }
function chaptersFrom(data: unknown, timings: unknown): Chapter[] {
  if (!Array.isArray(data)) return [];
  const timingMap = new Map<number, number>();
  if (Array.isArray(timings)) timings.forEach((item) => { if (item && typeof item === "object" && typeof (item as { beat_id?: unknown }).beat_id === "number" && typeof (item as { tl_start?: unknown }).tl_start === "number") timingMap.set((item as { beat_id: number }).beat_id, (item as { tl_start: number }).tl_start); });
  return data.flatMap((item, index) => item && typeof item === "object" ? [{ index, title: String((item as { title?: unknown }).title ?? `Chapter ${index + 1}`), episode_key: typeof (item as { episode_key?: unknown }).episode_key === "string" ? (item as { episode_key: string }).episode_key : undefined, start_s: timingMap.get(Number((item as { start_beat_id?: unknown }).start_beat_id)) ?? 0 }] : []);
}
function placementsFrom(data: unknown): EdlPlacement[] {
  if (!Array.isArray(data)) return [];
  return data.flatMap((item, index) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    if (typeof row.tl_start !== "number" || typeof row.tl_end !== "number") return [];
    const source = typeof row.src === "string" ? row.src : undefined;
    return [{ id: `${index}-${row.tl_start}`, tl_start: row.tl_start, tl_end: row.tl_end, beat_id: typeof row.beat_id === "number" ? row.beat_id : undefined, src: source, episode_key: source?.split("/")[0], src_in: typeof row.src_in === "number" ? row.src_in : undefined, src_out: typeof row.src_out === "number" ? row.src_out : undefined }];
  });
}

export const api = {
  health: async (): Promise<HealthResponse> => {
    const raw = await request<{ status: HealthResponse["status"]; runtime: ApiCheck[]; providers: Record<string, boolean>; active_job?: ApiJob | null }>("/health");
    return { status: raw.status, active_job_id: raw.active_job?.id ?? null, queue_depth: 0, providers: raw.providers, checks: raw.runtime.map((check) => ({ key: check.code, label: check.code.replaceAll("_", " "), status: check.status, detail: check.message, details: check.details })) };
  },
  presets: async (): Promise<Preset[]> => (await request<{ id: string; name: string; token: string; kind: "single" | "series"; description?: string; summary?: Record<string, unknown> }[]>("/presets")).map((item) => ({ ...item, path: item.name })),
  roots: () => request<FsRoot[]>("/fs/roots"),
  entries: (rootId: string, token?: string) => request<FsEntry[]>(`/fs/entries?${token ? `token=${encodeURIComponent(token)}` : `root_id=${encodeURIComponent(rootId)}`}`),
  listing: async (rootId: string, token?: string): Promise<FsListing> => {
    const query = token ? `token=${encodeURIComponent(token)}` : `root_id=${encodeURIComponent(rootId)}`;
    try {
      return await request<FsListing>(`/fs/listing?${query}`);
    } catch {
      const entries = await request<FsEntry[]>(`/fs/entries?${query}`);
      return { current: { root_id: rootId, token: token ?? "", name: rootId, at_root: !token }, entries };
    }
  },
  childToken: (parentToken: string, name: string) => request<{ token: string; name: string }>("/fs/child-token", { method: "POST", body: JSON.stringify({ parent_token: parentToken, name }) }),
  jobs: async (): Promise<Job[]> => (await request<ApiJob[]>("/jobs")).map((job) => mapJob(job)),
  job: async (id: string): Promise<Job> => { const raw = await request<{ job: ApiJob; stages: ApiStage[] }>(`/jobs/${encodeURIComponent(id)}`); return mapJob(raw.job, raw.stages); },
  createJob: async (planId: string): Promise<Job> => mapJob(await request<ApiJob>("/jobs", { method: "POST", body: JSON.stringify({ plan_id: planId }) })),
  resumeJob: async (id: string): Promise<Job> => mapJob(await request<ApiJob>(`/jobs/${encodeURIComponent(id)}/resume`, { method: "POST" })),
  cancelJob: async (id: string): Promise<Job> => mapJob(await request<ApiJob>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" })),
  rerunJob: async (id: string, scope: string, stage?: string): Promise<Job> => mapJob(await request<ApiJob>(`/jobs/${encodeURIComponent(id)}/rerun`, { method: "POST", body: JSON.stringify({ scope, stage, confirmed: scope === "all" }) })),
  events: async (id: string, after = 0): Promise<JobEvent[]> => (await request<ApiEvent[]>(`/jobs/${encodeURIComponent(id)}/events?after_seq=${after}&limit=5000`)).map(normalizeEvent),
  runs: async (): Promise<RunSummary[]> => (await request<ApiRun[]>("/runs")).map(mapRun),
  run: async (id: string): Promise<RunSummary> => mapRun(await request<ApiRun>(`/runs/${encodeURIComponent(id)}`)),
  registerRun: async (pathToken: string): Promise<RunSummary> => mapRun(await request<ApiRun>("/runs/register", { method: "POST", body: JSON.stringify({ path_token: pathToken }) })),
  episodes: async (runId: string): Promise<EpisodeSummary[]> => (await request<ApiEpisode[]>(`/runs/${encodeURIComponent(runId)}/episodes`)).map(mapEpisode),
  episode: async (runId: string, key: string): Promise<EpisodeDetail> => {
    const raw = await request<{ episode: ApiEpisode; film_map: unknown[]; story_map: unknown[]; review_script: unknown[]; beats_timing: unknown[]; shots: unknown[] }>(`/runs/${encodeURIComponent(runId)}/episodes/${encodeURIComponent(key)}`);
    return { ...mapEpisode(raw.episode), film_map: raw.film_map, review_beats: raw.review_script, timings: raw.beats_timing, shots: raw.shots };
  },
  artifacts: async (runId: string): Promise<Artifact[]> => (await request<ApiArtifact[]>(`/runs/${encodeURIComponent(runId)}/artifacts`)).map(mapArtifact),
  artifact: artifactContent,
  artifactUrl: (runId: string, artifactId: string, media = false) => `${API_ROOT}/runs/${encodeURIComponent(runId)}/${media ? "media" : "artifacts"}/${encodeURIComponent(artifactId)}`,
  qa: async (runId: string): Promise<RunQa> => {
    const [raw, run, artifacts] = await Promise.all([request<ApiQa>(`/runs/${encodeURIComponent(runId)}/qa`), request<ApiRun>(`/runs/${encodeURIComponent(runId)}`), request<ApiArtifact[]>(`/runs/${encodeURIComponent(runId)}/artifacts`)]);
    const find = (name: string) => artifacts.find((item) => item.name === name && raw.kind === "series" && item.relative_path.includes("series_recap")) ?? artifacts.find((item) => item.name === name);
    const [chapterData, timingData, edlData] = await Promise.all([find("series_chapters.json") ? artifactContent(runId, find("series_chapters.json")!.id) : undefined, find("beats_timing.json") ? artifactContent(runId, find("beats_timing.json")!.id) : undefined, find("edl.json") ? artifactContent(runId, find("edl.json")!.id) : undefined]);
    return { status: raw.status, metrics: qaMetrics(raw.checks, artifacts), blocker_codes: raw.checks.filter((check) => check.status === "block").map((check) => check.code), warnings: raw.checks.filter((check) => check.status === "warn").map((check) => check.message), durations: { composer_estimate_s: numberMetric(raw.metrics, "composer_estimated_duration_s"), tts_s: numberMetric(raw.metrics, "tts_duration_s"), edl_s: numberMetric(raw.metrics, "edl_duration_s"), render_s: numberMetric(raw.metrics, "render_duration_s"), minimum_s: firstNumberMetric(raw.metrics, "minimum_duration_s", "target_min_s", "target_total_min_s"), target_max_s: firstNumberMetric(raw.metrics, "maximum_duration_s", "target_max_s", "target_total_max_s"), hard_cap_s: numberMetric(raw.metrics, "hard_cap_s") }, chapters: chaptersFrom(chapterData, timingData), placements: placementsFrom(edlData), output_artifact_id: run.output_artifact_id ?? undefined };
  },
  inspectSingle: (sourceToken: string) => request<SingleSourceInspection>("/inspect/single", { method: "POST", body: JSON.stringify({ source_token: sourceToken }) }),
  inspectSeries: (manifestToken: string, episodes?: string) => request<SeriesSourceInspection>("/inspect/series", { method: "POST", body: JSON.stringify({ manifest_token: manifestToken, ...(episodes?.trim() ? { episodes: episodes.trim() } : {}) }) }),
  preflight: async (body: Record<string, unknown>): Promise<PreflightResponse> => { const raw = await request<{ can_start: boolean; checks: ApiCheck[]; warnings: string[]; providers?: Record<string, boolean>; plan_id?: string }>("/preflight", { method: "POST", body: JSON.stringify(body) }); return { can_start: raw.can_start, checks: raw.checks.map((check) => ({ key: check.code, label: check.label ?? check.code.replaceAll("_", " "), status: check.status, detail: check.message, details: check.details })), warnings: raw.warnings, providers: raw.providers, plan_id: raw.plan_id }; },
  plan: async (kind: "single" | "series", body: Record<string, unknown>): Promise<PlanResponse> => { const requestBody = { ...body }; delete requestBody.kind; const raw = await request<ApiPlan>(`/plans/${kind}`, { method: "POST", body: JSON.stringify(requestBody) }); return { plan_id: raw.plan_id, kind: raw.kind, command: raw.command, run_dir: raw.run_dir, stages: raw.dag.map(({ key, label, status, outputs }) => ({ key, label, status: status as StageState["status"], episode_key: key.includes(":") ? key.split(":", 1)[0] : undefined, outputs })), checks: raw.checks?.map((check) => ({ key: check.code, label: check.label ?? check.code.replaceAll("_", " "), status: check.status, detail: check.message, details: check.details })), output_paths: raw.output_paths, warnings: raw.warnings, can_start: raw.can_start, dry_run_output: raw.dry_run_output, expires_at: raw.created_at }; },
  testProfile: async (): Promise<RuntimeCheckResult> => {
    const raw = await request<{ status: "pass" | "warn" | "block"; composer_available: boolean; url?: string; sent: boolean }>("/health/chatgpt-profile", { method: "POST" });
    return { status: raw.status, detail: raw.sent ? "Profile probe unexpectedly submitted content." : raw.composer_available ? "Composer is available; no prompt was sent." : "ChatGPT composer is not available." };
  },
};

interface RuntimeCheckResult { status: "pass" | "warn" | "block"; detail?: string; }
