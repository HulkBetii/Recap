import { useQuery } from "@tanstack/react-query";
import { Download, FileJson, RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useLocale } from "../i18n";
import type { Artifact, EdlPlacement, JobEvent, RunQa } from "../types";
import { formatBytes, formatDuration, titleCase } from "../utils";
import { EmptyState, ErrorNotice, Skeleton, StatusPill } from "./Ui";
import styles from "./data.module.css";

const LOG_ROW_HEIGHT = 24;

export function LogViewer({ events, connected, onReconnect, terminal = false, persistedArtifactUrl }: { events: JobEvent[]; connected: boolean; onReconnect?: () => void; terminal?: boolean; persistedArtifactUrl?: string }) {
  const { t } = useLocale();
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [stageFilter, setStageFilter] = useState("all");
  const [levelFilter, setLevelFilter] = useState("all");
  const [followLatest, setFollowLatest] = useState(true);
  const [scrollTop, setScrollTop] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);
  const normalizedLogs = useMemo(() => events.flatMap((event) => {
    if (event.type !== "log") return [];
    const lines = Array.isArray(event.payload?.lines) ? event.payload.lines.filter((line): line is string => typeof line === "string") : [event.message ?? ""];
    const source = typeof event.payload?.source === "string" ? event.payload.source.split(/[\\/]/).at(-1) ?? "launcher" : "launcher";
    const stage = event.stage ?? (typeof event.payload?.stage === "string" ? event.payload.stage : "system");
    return lines.map((message, lineIndex) => ({ ...event, id: Number(`${event.id}${String(lineIndex).padStart(3, "0")}`), source, stage, message }));
  }), [events]);
  const options = useMemo(() => ({
    sources: [...new Set(normalizedLogs.map((event) => event.source))].sort(),
    stages: [...new Set(normalizedLogs.map((event) => event.stage ?? "system"))].sort(),
    levels: [...new Set(normalizedLogs.map((event) => event.level ?? "info"))].sort(),
  }), [normalizedLogs]);
  const logs = useMemo(() => normalizedLogs.filter((event) => {
    const haystack = `${event.source} ${event.stage ?? ""} ${event.message ?? ""}`.toLowerCase();
    return (sourceFilter === "all" || event.source === sourceFilter) && (stageFilter === "all" || event.stage === stageFilter) && (levelFilter === "all" || (event.level ?? "info") === levelFilter) && haystack.includes(search.toLowerCase());
  }), [levelFilter, normalizedLogs, search, sourceFilter, stageFilter]);
  const visibleCount = 18;
  const start = Math.max(0, Math.floor(scrollTop / LOG_ROW_HEIGHT) - 4);
  const end = Math.min(logs.length, start + visibleCount + 8);
  const visible = logs.slice(start, end);

  useEffect(() => {
    if (!followLatest || !viewport.current || logs.length === 0) return;
    viewport.current.scrollTop = viewport.current.scrollHeight;
    setScrollTop(viewport.current.scrollTop);
  }, [followLatest, logs.length]);

  const download = () => {
    const text = normalizedLogs.map((event) => `[${event.timestamp}] [${event.level ?? "info"}] [${event.source}]${event.stage ? ` [${event.stage}]` : ""} ${event.message}`).join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "recap-run.log";
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return <div className={styles.logWrap}>
    <div className={styles.logToolbar}><Search size={14} /><input aria-label={t("logs.filter")} className={styles.logSearch} placeholder={t("logs.filter")} value={search} onChange={(event) => setSearch(event.target.value)} /><select aria-label={t("logs.allSources")} className={styles.logSelect} value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="all">{t("logs.allSources")}</option>{options.sources.map((source) => <option key={source} value={source}>{t("qa.sourceField")} · {source}</option>)}</select><select aria-label={t("logs.allStages")} className={styles.logSelect} value={stageFilter} onChange={(event) => setStageFilter(event.target.value)}><option value="all">{t("logs.allStages")}</option>{options.stages.map((stage) => <option key={stage} value={stage}>{titleCase(stage)}</option>)}</select><select aria-label={t("logs.allLevels")} className={styles.logSelect} value={levelFilter} onChange={(event) => setLevelFilter(event.target.value)}><option value="all">{t("logs.allLevels")}</option>{options.levels.map((level) => <option key={level} value={level}>{titleCase(level)}</option>)}</select><button type="button" className={styles.logToolButton} aria-pressed={followLatest} onClick={() => setFollowLatest((value) => !value)}>{followLatest ? t("logs.following") : t("logs.followLatest")}</button>{!followLatest && <button type="button" className={styles.logToolButton} onClick={() => { setFollowLatest(true); if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight; }}>{t("logs.jumpLatest")}</button>}<button type="button" className={styles.logToolButton} onClick={onReconnect} disabled={!onReconnect}><RefreshCw size={13} />{t("logs.reconnect")}</button>{persistedArtifactUrl && <a className={styles.logToolButton} href={persistedArtifactUrl} download><FileJson size={13} />{t("logs.logFile")}</a>}<button type="button" className={styles.logToolButton} onClick={download} disabled={!normalizedLogs.length}><Download size={13} />{t("logs.download")}</button><span className={`${styles.connection} ${connected ? styles.connected : ""}`}>{terminal ? t("logs.complete") : connected ? t("logs.live") : t("logs.reconnecting")}</span></div>
    {logs.length === 0 ? <div className={styles.logEmpty}>{events.length ? t("logs.noMatch") : terminal ? t("logs.emptyComplete") : t("logs.waiting")}</div> : <div ref={viewport} className={styles.logViewport} onScroll={(event) => { setScrollTop(event.currentTarget.scrollTop); const target = event.currentTarget; if (followLatest && target.scrollHeight - target.scrollTop - target.clientHeight > LOG_ROW_HEIGHT * 2) setFollowLatest(false); }}><div style={{ height: logs.length * LOG_ROW_HEIGHT, position: "relative" }}>{visible.map((event, index) => <div key={`${event.id}-${index}`} className={`${styles.logRow} ${event.level === "warning" ? styles.logWarning : ""} ${event.level === "error" ? styles.logError : ""}`} style={{ top: (start + index) * LOG_ROW_HEIGHT }}><span className={styles.logTime}>{new Date(event.timestamp).toLocaleTimeString([], { hour12: false })}</span><span className={styles.logSource}>{event.source}</span><span className={styles.logStage}>{event.stage ?? t("logs.system")}</span><span className={styles.logMessage}>{event.message}</span></div>)}</div></div>}
  </div>;
}

export function ArtifactBrowser({ runId, artifacts }: { runId: string; artifacts: Artifact[] }) {
  const { t } = useLocale();
  const [selectedId, setSelectedId] = useState<string | undefined>(artifacts[0]?.id);
  const selected = artifacts.find((item) => item.id === selectedId) ?? artifacts[0];
  const preview = useQuery({ queryKey: ["artifact", runId, selected?.id], queryFn: () => api.artifact(runId, selected!.id), enabled: Boolean(selected && ["json", "text", "log", "html"].includes(selected.kind)) });
  if (!artifacts.length) return <EmptyState title={t("qa.noArtifact")} detail={t("qa.noArtifactDetail")} />;

  return <div className={styles.artifactLayout}>
    <div className={styles.artifactList}>{artifacts.map((artifact) => <button key={artifact.id} className={`${styles.artifactButton} ${selected?.id === artifact.id ? styles.artifactSelected : ""}`} onClick={() => setSelectedId(artifact.id)}><span className={styles.artifactName}>{artifact.name}</span><span className={styles.artifactMeta}>{titleCase(artifact.kind)} / {formatBytes(artifact.size)}</span></button>)}</div>
    <div className={styles.artifactPreview}>{selected && <><div className={styles.previewHead}><div><strong className={styles.artifactName}>{selected.name}</strong><div className={styles.artifactMeta}>{selected.stage ?? "run"} / {formatBytes(selected.size)}</div></div><a className={styles.downloadLink} href={api.artifactUrl(runId, selected.id, ["video", "audio", "image"].includes(selected.kind))} download><Download size={15} /><span>{t("qa.download")}</span></a></div><div className={styles.previewBody}><ArtifactPreview runId={runId} artifact={selected} data={preview.data} loading={preview.isLoading} error={preview.error} /></div></> }</div>
  </div>;
}

function ArtifactPreview({ runId, artifact, data, loading, error }: { runId: string; artifact: Artifact; data: unknown; loading: boolean; error: unknown }) {
  const { t } = useLocale();
  if (loading) return <Skeleton height={280} />;
  if (error) return <ErrorNotice error={error} />;
  const mediaUrl = api.artifactUrl(runId, artifact.id, true);
  if (artifact.kind === "video") return <video className={styles.media} src={mediaUrl} controls preload="metadata" />;
  if (artifact.kind === "audio") return <audio className={styles.media} src={mediaUrl} controls preload="metadata" />;
  if (artifact.kind === "image") return <img className={styles.image} src={mediaUrl} alt={artifact.name} />;
  if (artifact.kind === "html") return <iframe className={styles.iframe} srcDoc={typeof data === "string" ? data : ""} sandbox="" title={artifact.name} />;
  if (["json", "text", "log"].includes(artifact.kind)) return <pre className={styles.json}>{typeof data === "string" ? data : JSON.stringify(data ?? artifact.preview, null, 2)}</pre>;
  return <EmptyState title={t("qa.previewUnavailable")} detail={t("qa.previewUnavailableDetail")} action={<FileJson size={20} />} />;
}

export function QaPanel({ qa, runId }: { qa: RunQa; runId?: string }) {
  const { t } = useLocale();
  const durations = qa.durations;
  const belowMinimum = Boolean(durations?.render_s != null && durations.minimum_s != null && durations.render_s < durations.minimum_s);
  const blockers = qa.metrics.filter((metric) => metric.status === "block");
  const warnings = qa.metrics.filter((metric) => metric.status === "warn");
  const passed = qa.metrics.filter((metric) => metric.status === "pass");
  const blockerCount = blockers.length || qa.blocker_codes?.length || 0;
  const warningCount = warnings.length || qa.warnings?.length || 0;
  return <>
    <div className={`${styles.qaSummary} ${qa.status === "block" ? styles.qaSummaryBlock : qa.status === "warn" ? styles.qaSummaryWarn : styles.qaSummaryPass}`}><div><p className={styles.metricLabel}>{t("qa.deliveryGate")}</p><strong>{qa.status === "block" ? t("qa.blocked") : qa.status === "warn" ? t("qa.reviewWarnings") : qa.status === "pass" ? t("qa.ready") : t("qa.awaiting")}</strong><span>{blockerCount} {t("qa.blockers")} · {warningCount} {t("qa.warnings")}</span></div><StatusPill status={qa.status} /></div>
    {(qa.blocker_codes?.length || qa.warnings?.length) ? <div className={styles.qaMessages}>{qa.blocker_codes?.map((code) => <div className={styles.qaMessageBlock} key={code}><StatusPill status="block" label={code} /></div>)}{qa.warnings?.map((warning) => <div className={styles.qaMessageWarn} key={warning}><StatusPill status="warn" label={t("qa.warningLabel")} /><span>{warning}</span></div>)}</div> : null}
    {durations && <div className={styles.durationGrid}>
      <Duration label={t("qa.composerEstimate")} value={durations.composer_estimate_s} />
      <Duration label={t("qa.ttsActual")} value={durations.tts_s} />
      <Duration label={t("qa.edlActual")} value={durations.edl_s} />
      <Duration label={t("qa.renderActual")} value={durations.render_s} tone={belowMinimum ? "block" : "neutral"} />
      <Duration label={t("qa.requiredMinimum")} value={durations.minimum_s} />
      <Duration label={t("qa.preferredMaximum")} value={durations.target_max_s} />
      <Duration label={t("qa.planningReference")} value={durations.hard_cap_s} />
    </div>}
    {(blockers.length || warnings.length) > 0 && <div className={styles.metricGrid}>{[...blockers, ...warnings].map((metric) => <MetricCard key={metric.key} metric={metric} runId={runId} />)}</div>}
    {passed.length > 0 && <details className={styles.passedDetails}><summary>{t("qa.passedChecks", { count: passed.length })}</summary><div className={styles.metricGrid}>{passed.map((metric) => <MetricCard key={metric.key} metric={metric} runId={runId} />)}</div></details>}
    {qa.chapters && qa.chapters.length > 0 && <div className={styles.chapters}>{qa.chapters.map((chapter) => <div className={styles.chapter} key={`${chapter.index}-${chapter.start_s}`}><span className={styles.chapterIndex}>#{String(chapter.index + 1).padStart(2, "0")}</span><span className={styles.chapterTime}>{formatDuration(chapter.start_s)}</span><span className={styles.chapterTitle}>{chapter.title}</span><StatusPill status={chapter.status ?? "pass"} label={chapter.episode_key ?? "chapter"} /></div>)}</div>}
  </>;
}

function MetricCard({ metric, runId }: { metric: RunQa["metrics"][number]; runId?: string }) {
  const { t } = useLocale();
  const content = <article className={styles.metric}><div className={styles.metricTop}><p className={styles.metricLabel}>{metric.label}</p><StatusPill status={metric.status} /></div><p className={styles.metricValue}>{typeof metric.value === "boolean" ? (metric.value ? t("qa.yes") : t("qa.no")) : metric.value ?? "-"}</p>{metric.detail && <p className={styles.metricDetail}>{metric.detail}</p>}</article>;
  return runId && metric.artifact_id ? <a key={metric.key} href={api.artifactUrl(runId, metric.artifact_id)}>{content}</a> : <div key={metric.key}>{content}</div>;
}

function Duration({ label, value, tone = "neutral" }: { label: string; value?: number; tone?: "neutral" | "block" }) {
  return <div data-duration-tone={tone} className={`${styles.duration} ${tone === "block" ? styles.capBlock : ""}`}><span>{label}</span><strong>{formatDuration(value)}</strong></div>;
}

export function EdlTimeline({ placements }: { placements: EdlPlacement[] }) {
  const { t } = useLocale();
  const [selected, setSelected] = useState<EdlPlacement | undefined>(placements[0]);
  const end = Math.max(...placements.map((item) => item.tl_end), 1);
  if (!placements.length) return <EmptyState title={t("qa.noEdl")} detail={t("qa.noEdlDetail")} />;
  return <div style={{ overflowX: "auto" }}><div className={styles.timeline}><span className={styles.timelineScale} style={{ left: 0 }}>0:00</span><span className={styles.timelineScale} style={{ left: "50%" }}>{formatDuration(end / 2)}</span><span className={styles.timelineScale} style={{ right: 0 }}>{formatDuration(end)}</span><div className={styles.timelineTrack}>{placements.map((placement) => <button aria-label={t("qa.placement", { id: placement.id })} title={`${placement.episode_key ?? t("qa.source")} / ${formatDuration(placement.tl_start)}-${formatDuration(placement.tl_end)}`} key={placement.id} className={`${styles.placement} ${selected?.id === placement.id ? styles.placementActive : ""}`} style={{ left: `${placement.tl_start / end * 100}%`, width: `${Math.max((placement.tl_end - placement.tl_start) / end * 100, .15)}%` }} onClick={() => setSelected(placement)}><span className={styles.visuallyHidden}>{placement.episode_key ?? t("qa.source")}, {formatDuration(placement.tl_start)} - {formatDuration(placement.tl_end)}</span></button>)}</div>{selected && <div className={styles.placementDetail}><div><span>{t("qa.timeline")}</span><strong>{formatDuration(selected.tl_start)} - {formatDuration(selected.tl_end)}</strong></div><div><span>{t("qa.episode")}</span><strong>{selected.episode_key ?? "-"}</strong></div><div><span>{t("qa.sourceField")}</span><strong>{selected.src ? selected.src.split(/[\\/]/).at(-1) : "-"}</strong></div><div><span>QA</span><strong>{selected.warnings?.join(", ") || selected.status || t("qa.pass")}</strong></div></div>}</div></div>;
}

export function FinalVideo({ runId, artifactId, qa }: { runId: string; artifactId: string; qa?: RunQa }) {
  return <div><video className={styles.media} src={api.artifactUrl(runId, artifactId, true)} controls preload="metadata" />{qa?.chapters && <div className={styles.chapters}>{qa.chapters.map((chapter) => <div className={styles.chapter} key={chapter.index}><span className={styles.chapterIndex}>#{String(chapter.index + 1).padStart(2, "0")}</span><span className={styles.chapterTime}>{formatDuration(chapter.start_s)}</span><span className={styles.chapterTitle}>{chapter.title}</span><span /></div>)}</div>}</div>;
}
