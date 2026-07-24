import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Film, Play, RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useJobEvents } from "../api/events";
import { PageHeader } from "../components/AppShell";
import { ArtifactBrowser, EdlTimeline, FinalVideo, LogViewer, QaPanel } from "../components/DataViews";
import { Button, ConfirmDialog, EmptyState, ErrorNotice, Section, Skeleton, StatusPill } from "../components/Ui";
import type { Job, RunSummary, StageState } from "../types";
import { relativeTime, titleCase } from "../utils";
import styles from "./pages.module.css";

const SINGLE_STAGES = ["ingest", "review", "tts", "shots", "match", "render"];
const SERIES_COLUMNS = ["storymap", "episode_planner", "shots"];

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<"cancel" | "rerun" | null>(null);
  const runsQuery = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 5_000 });
  const jobsQuery = useQuery({ queryKey: ["jobs"], queryFn: api.jobs, refetchInterval: 5_000 });
  const initialRun = runsQuery.data?.find((item) => item.id === runId);
  const candidateJob = jobsQuery.data?.find((item) => item.id === runId || item.id === initialRun?.job_id);
  const jobQuery = useQuery({ queryKey: ["job", candidateJob?.id], queryFn: () => api.job(candidateJob!.id), enabled: Boolean(candidateJob?.id), refetchInterval: 4_000, retry: false });
  const job = jobQuery.data;
  const run = initialRun ?? runsQuery.data?.find((item) => item.job_id === job?.id);
  const resolvedRunId = run?.id ?? "";
  const episodes = useQuery({ queryKey: ["run", resolvedRunId, "episodes"], queryFn: () => api.episodes(resolvedRunId), enabled: Boolean(resolvedRunId), refetchInterval: job?.status === "running" ? 5_000 : false });
  const artifacts = useQuery({ queryKey: ["run", resolvedRunId, "artifacts"], queryFn: () => api.artifacts(resolvedRunId), enabled: Boolean(resolvedRunId), refetchInterval: job?.status === "running" ? 5_000 : false });
  const qa = useQuery({ queryKey: ["run", resolvedRunId, "qa"], queryFn: () => api.qa(resolvedRunId), enabled: Boolean(resolvedRunId), refetchInterval: job?.status === "running" ? 5_000 : false, retry: false });
  const history = useQuery({ queryKey: ["events", job?.id], queryFn: () => api.events(job!.id), enabled: Boolean(job?.id) });
  const live = useJobEvents(job?.id);
  const events = useMemo(() => {
    const map = new Map(history.data?.map((item) => [item.id, item]) ?? []);
    live.events.forEach((item) => map.set(item.id, item));
    return [...map.values()].sort((a, b) => a.id - b.id);
  }, [history.data, live.events]);
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ["job"] }); void queryClient.invalidateQueries({ queryKey: ["run", resolvedRunId] }); void queryClient.invalidateQueries({ queryKey: ["runs"] }); };
  const resume = useMutation({ mutationFn: () => api.resumeJob(job!.id), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => api.cancelJob(job!.id), onSuccess: () => { setConfirm(null); refresh(); } });
  const rerun = useMutation({ mutationFn: () => api.rerunJob(job!.id, run?.kind === "series" ? "final" : "stage", run?.kind === "single" ? job?.current_stage ?? "render" : undefined), onSuccess: () => { setConfirm(null); refresh(); } });
  const loading = runsQuery.isLoading || jobsQuery.isLoading || (Boolean(candidateJob) && jobQuery.isLoading);
  const error = runsQuery.error ?? jobQuery.error;

  if (loading) return <><PageHeader eyebrow="Control room" title="Loading run" subtitle="Reconciling process state and artifacts." /><Skeleton height={250} /></>;
  if ((error && !run && !job) || (!run && !job)) return <><PageHeader eyebrow="Control room" title="Run unavailable" subtitle="The operational record could not be loaded." /><ErrorNotice error={error ?? new Error("Run or job does not exist")} /></>;
  const display = run ?? jobToRun(job!, runId);
  const active = job && ["running", "starting", "cancel_requested"].includes(job.status);
  const resumable = job && ["failed", "interrupted", "orphaned", "cancelled", "blocked"].includes(job.status);

  return <>
    <PageHeader eyebrow={`${display.kind} operation`} title={display.title} subtitle={display.run_dir} actions={<div className={styles.actions}>{active && <Button variant="danger" onClick={() => setConfirm("cancel")}><Ban size={14} />Cancel</Button>}{resumable && <Button variant="primary" disabled={resume.isPending} onClick={() => resume.mutate()}><Play size={14} />Resume</Button>}{job?.status === "succeeded" && <Button onClick={() => setConfirm("rerun")}><RotateCcw size={14} />Rerun {display.kind === "series" ? "final" : "stage"}</Button>}</div>} />
    <div className={styles.detailGrid}>
      <div className={styles.heroPanel}><div className={styles.heroTop}><div><StatusPill status={job?.status ?? display.status} /><h2>{job?.current_stage ? titleCase(job.current_stage) : display.status === "succeeded" ? "Pipeline complete" : "Awaiting next action"}</h2><p className={styles.heroPath}>{display.run_dir}</p></div><StatusPill status={qa.data?.status ?? display.delivery_status ?? "unknown"} label={`Delivery ${qa.data?.status ?? display.delivery_status ?? "unknown"}`} /></div><div className={styles.heroStats}><HeroStat label="Episode scope" value={display.kind === "series" ? `${episodes.data?.length ?? display.episode_count ?? "-"} episodes` : "Single source"} /><HeroStat label="Heartbeat" value={relativeTime(job?.last_heartbeat)} /><HeroStat label="Attempt" value={`${Math.max(...(job?.stages?.map((stage) => stage.attempt ?? 1) ?? [1]))}`} /><HeroStat label="Exit code" value={job?.exit_code == null ? "-" : String(job.exit_code)} /></div></div>
      <Section eyebrow="Execution" title={display.kind === "series" ? "Final chain" : "Stage rail"}><StageRail stages={job?.stages ?? []} kind={display.kind} executionStatus={job?.status ?? display.status} deliveryStatus={qa.data?.status ?? display.delivery_status} /></Section>
    </div>
    {display.kind === "series" && <div style={{ marginBottom: 16 }}><Section eyebrow="Season" title="Episode matrix">{episodes.isLoading ? <Skeleton height={260} /> : <EpisodeMatrix runId={resolvedRunId} episodes={episodes.data ?? []} />}</Section></div>}
    <Tabs.Root defaultValue="qa">
      <Tabs.List className={styles.tabsList}><Tabs.Trigger className={styles.tab} value="qa">Delivery QA</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="edl">EDL timeline</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="logs">Live logs</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="artifacts">Artifacts</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="output">Output</Tabs.Trigger></Tabs.List>
      <Tabs.Content className={styles.tabPanel} value="qa"><Section eyebrow="Independent gate" title="Delivery quality">{qa.isLoading ? <Skeleton height={280} /> : qa.error ? <ErrorNotice error={qa.error} /> : qa.data ? <QaPanel qa={qa.data} runId={resolvedRunId} /> : <EmptyState title="QA not available" detail="Quality checks will appear as artifacts become valid." />}</Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="edl"><Section eyebrow="Read only" title="Edit decision list">{qa.data?.placements ? <EdlTimeline placements={qa.data.placements} /> : <EmptyState title="No timeline yet" detail="The EDL is indexed after the match stage completes." />}</Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="logs"><Section eyebrow="SSE stream" title="Process output"><LogViewer events={events} connected={live.connected} /></Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="artifacts"><Section eyebrow="Validated files" title="Artifact browser">{artifacts.isLoading ? <Skeleton height={430} /> : artifacts.error ? <ErrorNotice error={artifacts.error} /> : <ArtifactBrowser runId={resolvedRunId} artifacts={artifacts.data ?? []} />}</Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="output"><Section eyebrow="Final delivery" title="Rendered recap">{qa.data?.output_artifact_id || display.output_artifact_id ? <FinalVideo runId={resolvedRunId} artifactId={qa.data?.output_artifact_id ?? display.output_artifact_id!} qa={qa.data} /> : <EmptyState title="No final video" detail="The output player activates once render and media validation finish." action={<Film size={23} />} />}</Section></Tabs.Content>
    </Tabs.Root>
    <ConfirmDialog open={confirm === "cancel"} title="Cancel this pipeline?" detail="The worker will send CTRL_BREAK, wait for a clean stop, then terminate only the verified process tree if needed." confirmLabel="Request cancellation" danger onConfirm={() => cancel.mutate()} onOpenChange={(open) => !open && setConfirm(null)} />
    <ConfirmDialog open={confirm === "rerun"} title={`Rerun ${display.kind === "series" ? "the final chain" : "this stage"}?`} detail="Existing valid artifacts are preserved according to CLI force semantics. The new attempt enters the FIFO queue." confirmLabel="Create rerun" onConfirm={() => rerun.mutate()} onOpenChange={(open) => !open && setConfirm(null)} />
  </>;
}

function jobToRun(job: Job, id: string): RunSummary { return { id, job_id: job.id, kind: job.kind, title: job.title ?? `Run ${id.slice(0, 8)}`, run_dir: job.run_dir, status: job.status, delivery_status: job.delivery_status, current_stage: job.current_stage }; }
function HeroStat({ label, value }: { label: string; value: string }) { return <div className={styles.heroStat}><span>{label}</span><strong>{value}</strong></div>; }

function StageRail({ stages, kind, executionStatus, deliveryStatus }: { stages: StageState[]; kind: "single" | "series"; executionStatus: string; deliveryStatus?: string }) {
  const keys = kind === "series" ? ["series_composer", "tts", "youtube_chapters", "series_match", "render", "delivery_qa"] : SINGLE_STAGES;
  return <div className={styles.stageRail}>{keys.map((key, index) => {
    const state = stages.find((item) => item.key === key && !item.episode_key);
    const inferred = key === "delivery_qa"
      ? deliveryStatus === "block" ? "blocked" : deliveryStatus === "warn" ? "warning" : deliveryStatus === "pass" ? "succeeded" : "pending"
      : executionStatus === "succeeded" ? "succeeded" : "pending";
    const status = state?.status ?? inferred;
    return <div className={styles.stageRow} key={key}><div className={styles.stageInfo}><span className={styles.stageIndex}>{String(index + 1).padStart(2, "0")}</span><div><strong>{titleCase(key)}</strong><small>{state?.message ?? (status === "skipped_valid" ? "Artifact already valid" : state ? "Pipeline stage" : "Inferred from validated run artifacts")}</small></div></div><StatusPill status={status} /></div>;
  })}</div>;
}

function EpisodeMatrix({ runId, episodes }: { runId: string; episodes: Awaited<ReturnType<typeof api.episodes>> }) {
  if (!episodes.length) return <EmptyState title="No episodes indexed" detail="Episode artifacts will populate this matrix as the series worker advances." />;
  return <div className={styles.matrixWrap}><table className={styles.matrix}><thead><tr><th>Episode</th><th>Translation</th><th>Timecodes</th>{SERIES_COLUMNS.map((column) => <th key={column}>{titleCase(column)}</th>)}</tr></thead><tbody>{episodes.map((episode) => <tr key={episode.key}><td><Link className={styles.episodeLink} to={`/runs/${runId}/episodes/${episode.key}`}>{episode.title ?? `Episode ${episode.number ?? episode.key}`}</Link></td><td>{episode.translation_ratio == null ? "-" : `${Math.round(episode.translation_ratio * 100)}%`}</td><td><StatusPill status={episode.approximate_timecodes ? "block" : "pass"} label={episode.approximate_timecodes ? "Approx" : "Strict"} /></td>{SERIES_COLUMNS.map((column) => <td key={column}><span title={episode.stages.find((stage) => stage.key === column)?.status ?? "pending"} className={styles.stageDot} data-status={episode.stages.find((stage) => stage.key === column)?.status ?? "pending"} /></td>)}</tr>)}</tbody></table></div>;
}
