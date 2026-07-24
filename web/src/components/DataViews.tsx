import { useQuery } from "@tanstack/react-query";
import { Download, FileJson, Search } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { Artifact, EdlPlacement, JobEvent, RunQa } from "../types";
import { formatBytes, formatDuration, titleCase } from "../utils";
import { Button, EmptyState, ErrorNotice, Skeleton, StatusPill } from "./Ui";
import styles from "./data.module.css";

const LOG_ROW_HEIGHT = 24;

export function LogViewer({ events, connected }: { events: JobEvent[]; connected: boolean }) {
  const [search, setSearch] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);
  const logs = useMemo(() => events.flatMap((event) => {
    if (event.type !== "log") return [];
    const lines = Array.isArray(event.payload?.lines) ? event.payload.lines.filter((line): line is string => typeof line === "string") : [event.message ?? ""];
    const source = typeof event.payload?.source === "string" ? event.payload.source.split(/[\\/]/).at(-1) : event.stage;
    return lines.map((message, lineIndex) => ({ ...event, id: Number(`${event.id}${String(lineIndex).padStart(3, "0")}`), stage: source, message }));
  }).filter((event) => `${event.stage ?? ""} ${event.message ?? ""}`.toLowerCase().includes(search.toLowerCase())), [events, search]);
  const visibleCount = 18;
  const start = Math.max(0, Math.floor(scrollTop / LOG_ROW_HEIGHT) - 4);
  const end = Math.min(logs.length, start + visibleCount + 8);
  const visible = logs.slice(start, end);

  return <div className={styles.logWrap}>
    <div className={styles.logToolbar}><Search size={14} /><input aria-label="Filter logs" className={styles.logSearch} placeholder="Filter stage or message" value={search} onChange={(event) => setSearch(event.target.value)} /><span className={`${styles.connection} ${connected ? styles.connected : ""}`}>{connected ? "Live" : "Reconnecting"}</span></div>
    {logs.length === 0 ? <div className={styles.logEmpty}>{events.length ? "No log lines match the filter" : "Waiting for pipeline output..."}</div> : <div ref={viewport} className={styles.logViewport} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}><div style={{ height: logs.length * LOG_ROW_HEIGHT, position: "relative" }}>{visible.map((event, index) => <div key={`${event.id}-${index}`} className={`${styles.logRow} ${event.level === "warning" ? styles.logWarning : ""} ${event.level === "error" ? styles.logError : ""}`} style={{ top: (start + index) * LOG_ROW_HEIGHT }}><span className={styles.logTime}>{new Date(event.timestamp).toLocaleTimeString([], { hour12: false })}</span><span className={styles.logStage}>{event.stage ?? "system"}</span><span className={styles.logMessage}>{event.message}</span></div>)}</div></div>}
  </div>;
}

export function ArtifactBrowser({ runId, artifacts }: { runId: string; artifacts: Artifact[] }) {
  const [selectedId, setSelectedId] = useState<string | undefined>(artifacts[0]?.id);
  const selected = artifacts.find((item) => item.id === selectedId) ?? artifacts[0];
  const preview = useQuery({ queryKey: ["artifact", runId, selected?.id], queryFn: () => api.artifact(runId, selected!.id), enabled: Boolean(selected && ["json", "text", "log", "html"].includes(selected.kind)) });
  if (!artifacts.length) return <EmptyState title="No artifacts indexed" detail="Artifacts appear here as soon as the worker validates output files." />;

  return <div className={styles.artifactLayout}>
    <div className={styles.artifactList}>{artifacts.map((artifact) => <button key={artifact.id} className={`${styles.artifactButton} ${selected?.id === artifact.id ? styles.artifactSelected : ""}`} onClick={() => setSelectedId(artifact.id)}><span className={styles.artifactName}>{artifact.name}</span><span className={styles.artifactMeta}>{titleCase(artifact.kind)} / {formatBytes(artifact.size)}</span></button>)}</div>
    <div className={styles.artifactPreview}>{selected && <><div className={styles.previewHead}><div><strong className={styles.artifactName}>{selected.name}</strong><div className={styles.artifactMeta}>{selected.stage ?? "run"} / {formatBytes(selected.size)}</div></div><a href={api.artifactUrl(runId, selected.id, ["video", "audio", "image"].includes(selected.kind))} download><Button iconOnly title="Download artifact"><Download size={15} /></Button></a></div><div className={styles.previewBody}><ArtifactPreview runId={runId} artifact={selected} data={preview.data} loading={preview.isLoading} error={preview.error} /></div></> }</div>
  </div>;
}

function ArtifactPreview({ runId, artifact, data, loading, error }: { runId: string; artifact: Artifact; data: unknown; loading: boolean; error: unknown }) {
  if (loading) return <Skeleton height={280} />;
  if (error) return <ErrorNotice error={error} />;
  const mediaUrl = api.artifactUrl(runId, artifact.id, true);
  if (artifact.kind === "video") return <video className={styles.media} src={mediaUrl} controls preload="metadata" />;
  if (artifact.kind === "audio") return <audio className={styles.media} src={mediaUrl} controls preload="metadata" />;
  if (artifact.kind === "image") return <img className={styles.image} src={mediaUrl} alt={artifact.name} />;
  if (artifact.kind === "html") return <iframe className={styles.iframe} srcDoc={typeof data === "string" ? data : ""} sandbox="" title={artifact.name} />;
  if (["json", "text", "log"].includes(artifact.kind)) return <pre className={styles.json}>{typeof data === "string" ? data : JSON.stringify(data ?? artifact.preview, null, 2)}</pre>;
  return <EmptyState title="Preview unavailable" detail="Download this artifact to inspect it with its native application." action={<FileJson size={20} />} />;
}

export function QaPanel({ qa, runId }: { qa: RunQa; runId?: string }) {
  const durations = qa.durations;
  const belowMinimum = Boolean(durations?.render_s != null && durations.minimum_s != null && durations.render_s < durations.minimum_s);
  return <>
    {durations && <div className={styles.durationGrid}>
      <Duration label="Composer estimate" value={durations.composer_estimate_s} />
      <Duration label="TTS actual" value={durations.tts_s} />
      <Duration label="EDL actual" value={durations.edl_s} />
      <Duration label="Render actual" value={durations.render_s} tone={belowMinimum ? "block" : "neutral"} />
      <Duration label="Required minimum" value={durations.minimum_s} />
      <Duration label="Preferred maximum" value={durations.target_max_s} />
      <Duration label="Planning reference" value={durations.hard_cap_s} />
    </div>}
    <div className={styles.metricGrid}>{qa.metrics.map((metric) => { const content = <article className={styles.metric}><div className={styles.metricTop}><p className={styles.metricLabel}>{metric.label}</p><StatusPill status={metric.status} /></div><p className={styles.metricValue}>{typeof metric.value === "boolean" ? (metric.value ? "Yes" : "No") : metric.value ?? "-"}</p>{metric.detail && <p className={styles.metricDetail}>{metric.detail}</p>}</article>; return runId && metric.artifact_id ? <a key={metric.key} href={api.artifactUrl(runId, metric.artifact_id)}>{content}</a> : <div key={metric.key}>{content}</div>; })}</div>
    {qa.chapters && qa.chapters.length > 0 && <div className={styles.chapters}>{qa.chapters.map((chapter) => <div className={styles.chapter} key={`${chapter.index}-${chapter.start_s}`}><span className={styles.chapterIndex}>#{String(chapter.index + 1).padStart(2, "0")}</span><span className={styles.chapterTime}>{formatDuration(chapter.start_s)}</span><span className={styles.chapterTitle}>{chapter.title}</span><StatusPill status={chapter.status ?? "pass"} label={chapter.episode_key ?? "chapter"} /></div>)}</div>}
  </>;
}

function Duration({ label, value, tone = "neutral" }: { label: string; value?: number; tone?: "neutral" | "block" }) {
  return <div data-duration-tone={tone} className={`${styles.duration} ${tone === "block" ? styles.capBlock : ""}`}><span>{label}</span><strong>{formatDuration(value)}</strong></div>;
}

export function EdlTimeline({ placements }: { placements: EdlPlacement[] }) {
  const [selected, setSelected] = useState<EdlPlacement | undefined>(placements[0]);
  const end = Math.max(...placements.map((item) => item.tl_end), 1);
  if (!placements.length) return <EmptyState title="No EDL placements" detail="The read-only timeline becomes available after matching completes." />;
  return <div style={{ overflowX: "auto" }}><div className={styles.timeline}><span className={styles.timelineScale} style={{ left: 0 }}>0:00</span><span className={styles.timelineScale} style={{ left: "50%" }}>{formatDuration(end / 2)}</span><span className={styles.timelineScale} style={{ right: 0 }}>{formatDuration(end)}</span><div className={styles.timelineTrack}>{placements.map((placement) => <button aria-label={`Placement ${placement.id}`} title={`${placement.episode_key ?? "source"} / ${formatDuration(placement.tl_start)}-${formatDuration(placement.tl_end)}`} key={placement.id} className={`${styles.placement} ${selected?.id === placement.id ? styles.placementActive : ""}`} style={{ left: `${placement.tl_start / end * 100}%`, width: `${Math.max((placement.tl_end - placement.tl_start) / end * 100, .15)}%` }} onClick={() => setSelected(placement)} />)}</div>{selected && <div className={styles.placementDetail}><div><span>Timeline</span><strong>{formatDuration(selected.tl_start)} - {formatDuration(selected.tl_end)}</strong></div><div><span>Episode</span><strong>{selected.episode_key ?? "-"}</strong></div><div><span>Source</span><strong>{selected.src ? selected.src.split(/[\\/]/).at(-1) : "-"}</strong></div><div><span>QA</span><strong>{selected.warnings?.join(", ") || selected.status || "pass"}</strong></div></div>}</div></div>;
}

export function FinalVideo({ runId, artifactId, qa }: { runId: string; artifactId: string; qa?: RunQa }) {
  return <div><video className={styles.media} src={api.artifactUrl(runId, artifactId, true)} controls preload="metadata" />{qa?.chapters && <div className={styles.chapters}>{qa.chapters.map((chapter) => <div className={styles.chapter} key={chapter.index}><span className={styles.chapterIndex}>#{String(chapter.index + 1).padStart(2, "0")}</span><span className={styles.chapterTime}>{formatDuration(chapter.start_s)}</span><span className={styles.chapterTitle}>{chapter.title}</span><span /></div>)}</div>}</div>;
}
