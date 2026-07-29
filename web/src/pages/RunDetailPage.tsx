import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, FileArchive, Film, Play, Radio, RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useJobEvents } from "../api/events";
import { PageHeader } from "../components/AppShell";
import { ArtifactBrowser, EdlTimeline, FinalVideo, LogViewer, QaPanel } from "../components/DataViews";
import { Button, ConfirmDialog, EmptyState, ErrorNotice, Section, Skeleton, StatusPill, selectClass } from "../components/Ui";
import { useLocale } from "../i18n";
import type { Job, RunSummary, StageState } from "../types";
import { relativeTime, titleCase } from "../utils";
import styles from "./pages.module.css";

const SINGLE_STAGES = ["ingest", "review", "tts", "shots", "match", "render"];
const SERIES_COLUMNS = ["storymap", "episode_planner", "shots"];

export function RunDetailPage() {
  const { t } = useLocale();
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<"cancel" | "rerun" | null>(null);
  const [rerunStage, setRerunStage] = useState("render");
  const [seriesRerunScope, setSeriesRerunScope] = useState<"final" | "all">("final");
  const [activeTab, setActiveTab] = useState("qa");
  const [reconnectToken, setReconnectToken] = useState(0);
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
  const live = useJobEvents(job?.id, reconnectToken);
  const events = useMemo(() => {
    const map = new Map(history.data?.map((item) => [item.id, item]) ?? []);
    live.events.forEach((item) => map.set(item.id, item));
    return [...map.values()].sort((a, b) => a.id - b.id);
  }, [history.data, live.events]);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["job"] });
    void queryClient.invalidateQueries({ queryKey: ["run", resolvedRunId] });
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };
  const navigateToAttempt = (nextJob: Job) => {
    setConfirm(null);
    refresh();
    navigate(`/runs/${nextJob.id}`);
  };
  const resume = useMutation({ mutationFn: () => api.resumeJob(job!.id), onSuccess: navigateToAttempt });
  const cancel = useMutation({ mutationFn: () => api.cancelJob(job!.id), onSuccess: () => { setConfirm(null); refresh(); } });
  const rerun = useMutation({ mutationFn: () => api.rerunJob(job!.id, run?.kind === "series" ? seriesRerunScope : "stage", run?.kind === "single" ? rerunStage : undefined), onSuccess: navigateToAttempt });
  const loading = runsQuery.isLoading || jobsQuery.isLoading || (Boolean(candidateJob) && jobQuery.isLoading);
  const error = runsQuery.error ?? jobQuery.error;

  if (loading) return <><PageHeader eyebrow={t("detail.controlRoom")} title={t("detail.loadingRun")} subtitle={t("detail.loadingRunDetail")} /><Skeleton height={250} /></>;
  if ((error && !run && !job) || (!run && !job)) return <><PageHeader eyebrow={t("detail.controlRoom")} title={t("detail.unavailableTitle")} subtitle={t("detail.unavailableDetail")} /><ErrorNotice error={error ?? new Error(t("detail.unavailableDetail"))} /></>;
  const display = run ?? jobToRun(job!, runId);
  const managed = Boolean(job);
  const cancelable = job && ["queued", "starting", "running", "cancel_requested"].includes(job.status);
  const resumable = job && ["failed", "interrupted", "orphaned", "cancelled", "blocked"].includes(job.status);
  const attempt = Math.max(...(job?.stages?.map((stage) => stage.attempt ?? 1) ?? [1]));
  const outputArtifactId = qa.data?.output_artifact_id ?? display.output_artifact_id;
  const persistedLog = artifacts.data?.find((artifact) => artifact.kind === "log" || artifact.name.endsWith(".log"));
  const mutationError = resume.error ?? rerun.error ?? cancel.error;
  const rerunStageIndex = SINGLE_STAGES.indexOf(rerunStage);
  const rerunStages = rerunStageIndex >= 0 ? SINGLE_STAGES.slice(rerunStageIndex).map(titleCase).join(" → ") : titleCase(rerunStage);
  const terminal = Boolean(job && ["cancelled", "succeeded", "failed", "interrupted", "orphaned", "blocked"].includes(job.status));
  const deliveryState = qa.data?.status ?? display.delivery_status ?? "unknown";
  const deliveryLabel = deliveryState === "block" ? t("qa.blocked") : deliveryState === "warn" ? t("qa.reviewWarnings") : deliveryState === "pass" ? t("qa.ready") : t("qa.awaiting");

  return <>
    <PageHeader eyebrow={`${display.kind} ${managed ? t("detail.operation") : t("detail.artifactRun")}`} title={display.title} subtitle={display.run_dir} actions={managed ? <div className={styles.actions}>{cancelable && <Button variant="danger" disabled={job.status === "cancel_requested" || cancel.isPending} onClick={() => setConfirm("cancel")}><Ban size={14} />{job.status === "queued" ? t("detail.removeQueue") : t("detail.cancel")}</Button>}{resumable && <Button variant="primary" disabled={resume.isPending} onClick={() => resume.mutate()}><Play size={14} />{t("detail.resume")}</Button>}{job?.status === "succeeded" && <div className={styles.rerunControls}><label><span className={styles.visuallyHidden}>{display.kind === "series" ? t("detail.rerunScopeLabel") : t("detail.rerunStageLabel")}</span><select className={selectClass} value={display.kind === "series" ? seriesRerunScope : rerunStage} onChange={(event) => display.kind === "series" ? setSeriesRerunScope(event.target.value as "final" | "all") : setRerunStage(event.target.value)}>{display.kind === "series" ? <><option value="final">{t("detail.finalChain")}</option><option value="all">{t("detail.entireSeason")}</option></> : SINGLE_STAGES.map((stage) => <option value={stage} key={stage}>{titleCase(stage)}</option>)}</select></label><Button onClick={() => setConfirm("rerun")}><RotateCcw size={14} />{t("detail.rerun")}</Button></div>}</div> : <StatusPill status="unknown" label={t("detail.readOnly")} />} />
    {(job?.error || mutationError) && <div className={styles.inlineNotice}><ErrorNotice error={mutationError ?? new Error(job!.error!)} /></div>}
    <div className={managed ? styles.detailGrid : styles.detailStandalone}>
      <div className={styles.heroPanel}><div className={styles.heroTop}><div><StatusPill status={managed ? job!.status : "unknown"} label={managed ? undefined : t("detail.artifactOnly")} /><h2>{managed ? job?.current_stage ? titleCase(job.current_stage) : display.status === "succeeded" ? t("detail.pipelineComplete") : display.status === "queued" ? t("detail.queueWaiting") : t("detail.awaitingAction") : t("detail.noTelemetry")}</h2><p className={styles.heroPath}>{display.run_dir}</p></div><StatusPill status={deliveryState} label={`${t("detail.delivery")}: ${deliveryLabel}`} /></div><div className={styles.heroStats}><HeroStat label={t("detail.episodeScope")} value={display.kind === "series" ? `${episodes.data?.length ?? display.episode_count ?? "-"} ${t("detail.episodes")}` : t("detail.singleSource")} />{managed ? <><HeroStat label={t("detail.heartbeat")} value={relativeTime(job?.last_heartbeat)} /><HeroStat label={t("detail.attempt")} value={String(attempt)} /><HeroStat label={t("detail.exitCode")} value={job?.exit_code == null ? "-" : String(job.exit_code)} /></> : <><HeroStat label={t("detail.management")} value={t("detail.artifactOnly")} /><HeroStat label={t("detail.indexed")} value={relativeTime(display.updated_at)} /><HeroStat label={t("detail.workerActions")} value={t("detail.unavailable")} /></>}</div><div className={styles.heroShortcuts}>{outputArtifactId && <Button variant="primary" onClick={() => setActiveTab("output")}><Film size={14} />{t("detail.playOutput")}</Button>}<Button variant="ghost" onClick={() => setActiveTab("artifacts")}><FileArchive size={14} />{t("detail.artifacts")}</Button>{managed && <Button variant="ghost" onClick={() => setActiveTab("logs")}><Radio size={14} />{t("detail.logs")}</Button>}</div></div>
      {managed && <Section eyebrow={t("detail.execution")} title={display.kind === "series" ? t("detail.finalChain") : t("detail.stageRail")}><StageRail stages={job?.stages ?? []} kind={display.kind} executionStatus={job?.status ?? display.status} deliveryStatus={qa.data?.status ?? display.delivery_status} /></Section>}
    </div>
    {display.kind === "series" && <div style={{ marginBottom: 16 }}><Section eyebrow={t("detail.season")} title={t("detail.episodeMatrix")}>{episodes.isLoading ? <Skeleton height={260} /> : <EpisodeMatrix runId={resolvedRunId} episodes={episodes.data ?? []} />}</Section></div>}
    <Tabs.Root value={activeTab} onValueChange={setActiveTab}>
      <Tabs.List className={styles.tabsList}><Tabs.Trigger className={styles.tab} value="qa">{t("detail.deliveryQA")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="edl">{t("detail.edl")}</Tabs.Trigger>{managed && <Tabs.Trigger className={styles.tab} value="logs">{t("detail.liveLogs")}</Tabs.Trigger>}<Tabs.Trigger className={styles.tab} value="artifacts">{t("detail.artifacts")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="output">{t("detail.output")}</Tabs.Trigger></Tabs.List>
      <Tabs.Content className={styles.tabPanel} value="qa"><Section eyebrow={t("detail.independentGate")} title={t("detail.deliveryQuality")}>{qa.isLoading ? <Skeleton height={280} /> : qa.error ? <ErrorNotice error={qa.error} /> : qa.data ? <QaPanel qa={qa.data} runId={resolvedRunId} /> : <EmptyState title={t("detail.qaUnavailable")} detail={t("detail.qaUnavailableDetail")} />}</Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="edl"><Section eyebrow={t("detail.readOnlyEyebrow")} title={t("detail.editDecisionList")}>{qa.data?.placements ? <EdlTimeline placements={qa.data.placements} /> : <EmptyState title={t("detail.noTimeline")} detail={t("detail.noTimelineDetail")} />}</Section></Tabs.Content>
      {managed && <Tabs.Content className={styles.tabPanel} value="logs"><Section eyebrow={t("detail.sseStream")} title={t("detail.processOutput")}><LogViewer events={events} connected={live.connected} terminal={terminal} persistedArtifactUrl={persistedLog ? api.artifactUrl(resolvedRunId, persistedLog.id) : undefined} onReconnect={() => setReconnectToken((value) => value + 1)} /></Section></Tabs.Content>}
      <Tabs.Content className={styles.tabPanel} value="artifacts"><Section eyebrow={t("detail.validatedFiles")} title={t("detail.artifacts")}>{artifacts.isLoading ? <Skeleton height={430} /> : artifacts.error ? <ErrorNotice error={artifacts.error} /> : <ArtifactBrowser runId={resolvedRunId} artifacts={artifacts.data ?? []} />}</Section></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="output"><Section eyebrow={t("detail.output")} title={t("detail.renderedRecap")}>{outputArtifactId ? <FinalVideo runId={resolvedRunId} artifactId={outputArtifactId} qa={qa.data} /> : <EmptyState title={t("detail.noVideo")} detail={t("detail.noVideoDetail")} action={<Film size={23} />} />}</Section></Tabs.Content>
    </Tabs.Root>
    <ConfirmDialog open={confirm === "cancel"} title={job?.status === "queued" ? t("detail.cancelQueuedTitle") : t("detail.cancelTitle")} detail={job?.status === "queued" ? t("detail.cancelQueuedDetail") : t("detail.cancelRunningDetail")} confirmLabel={job?.status === "queued" ? t("detail.cancelQueued") : t("detail.requestCancellation")} danger onConfirm={() => cancel.mutate()} onOpenChange={(open) => !open && setConfirm(null)} />
    <ConfirmDialog open={confirm === "rerun"} title={display.kind === "series" && seriesRerunScope === "all" ? t("detail.rerunAllTitle") : `${t("detail.rerun")} ${display.kind === "series" ? t("detail.finalChain") : titleCase(rerunStage)}?`} detail={display.kind === "series" && seriesRerunScope === "all" ? t("detail.rerunAllDetail") : display.kind === "single" ? t("detail.rerunSingleDetail", { stage: titleCase(rerunStage), stages: rerunStages }) : t("detail.rerunSeriesDetail")} confirmLabel={display.kind === "series" && seriesRerunScope === "all" ? t("detail.forceSeason") : t("detail.rerunConfirm")} danger={display.kind === "series" && seriesRerunScope === "all"} onConfirm={() => rerun.mutate()} onOpenChange={(open) => !open && setConfirm(null)} />
  </>;
}

function jobToRun(job: Job, id: string): RunSummary { return { id, job_id: job.id, kind: job.kind, title: job.title ?? `Run ${id.slice(0, 8)}`, run_dir: job.run_name, status: job.status, delivery_status: job.delivery_status, current_stage: job.current_stage }; }
function HeroStat({ label, value }: { label: string; value: string }) { return <div className={styles.heroStat}><span>{label}</span><strong>{value}</strong></div>; }

function StageRail({ stages, kind, executionStatus, deliveryStatus }: { stages: StageState[]; kind: "single" | "series"; executionStatus: string; deliveryStatus?: string }) {
  const { t } = useLocale();
  const keys = kind === "series" ? ["series_composer", "tts", "youtube_chapters", "series_match", "render", "delivery_qa"] : SINGLE_STAGES;
  return <div className={styles.stageRail}>{keys.map((key, index) => {
    const state = stages.find((item) => item.key === key && !item.episode_key);
    const inferred = key === "delivery_qa"
      ? deliveryStatus === "block" ? "blocked" : deliveryStatus === "warn" ? "warning" : deliveryStatus === "pass" ? "succeeded" : "pending"
      : executionStatus === "succeeded" ? "succeeded" : "pending";
    const status = state?.status ?? inferred;
    return <div className={styles.stageRow} key={key}><div className={styles.stageInfo}><span className={styles.stageIndex}>{String(index + 1).padStart(2, "0")}</span><div><strong>{titleCase(key)}</strong><small>{state?.message ?? (status === "skipped_valid" ? t("detail.artifactAlreadyValid") : state ? t("detail.pipelineStage") : t("detail.inferredArtifacts"))}</small></div></div><StatusPill status={status} /></div>;
  })}</div>;
}

function EpisodeMatrix({ runId, episodes }: { runId: string; episodes: Awaited<ReturnType<typeof api.episodes>> }) {
  const { t } = useLocale();
  if (!episodes.length) return <EmptyState title={t("detail.noEpisodes")} detail={t("detail.noEpisodesDetail")} />;
  return <div className={styles.matrixWrap}><table className={styles.matrix}><thead><tr><th>{t("detail.episodeScope")}</th><th>{t("detail.translation")}</th><th>{t("detail.timecodes")}</th>{SERIES_COLUMNS.map((column) => <th key={column}>{titleCase(column)}</th>)}</tr></thead><tbody>{episodes.map((episode) => <tr key={episode.key}><td><Link className={styles.episodeLink} to={`/runs/${runId}/episodes/${episode.key}`}>{episode.title ?? `Episode ${episode.number ?? episode.key}`}</Link></td><td>{episode.translation_ratio == null ? "-" : `${Math.round(episode.translation_ratio * 100)}%`}</td><td><StatusPill status={episode.approximate_timecodes ? "block" : "pass"} label={episode.approximate_timecodes ? t("detail.approx") : t("detail.strict")} /></td>{SERIES_COLUMNS.map((column) => { const status = episode.stages.find((stage) => stage.key === column)?.status ?? "pending"; return <td key={column}><span title={titleCase(status)} className={styles.stageDot} data-status={status} role="img" aria-label={`${titleCase(column)}: ${titleCase(status)}`}><span className={styles.visuallyHidden}>{titleCase(status)}</span></span></td>; })}</tr>)}</tbody></table></div>;
}
