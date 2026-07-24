import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Clapperboard, Layers3, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, EmptyState, ErrorNotice, Skeleton, StatusPill } from "../components/Ui";
import type { RunSummary } from "../types";
import { relativeTime, titleCase } from "../utils";
import styles from "./pages.module.css";

export function RunsPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 10_000 });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 5_000 });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.jobs, refetchInterval: 5_000 });
  const active = jobs.data?.find((job) => ["running", "starting", "cancel_requested"].includes(job.status));
  const pendingJobs = jobs.data?.filter((job) => ["queued", "starting", "running", "cancel_requested"].includes(job.status)) ?? [];
  const queue = jobs.data?.filter((job) => job.status === "queued").length ?? health.data?.queue_depth ?? 0;

  return <>
    <PageHeader eyebrow="Operations" title="Pipeline runs" subtitle="Launch, resume and inspect local recap jobs without leaving the artifact-first pipeline." actions={<Link to="/runs/new"><Button variant="primary" large><Plus size={16} />New run</Button></Link>} />
    <div className={styles.healthStrip}>
      <HealthCell label="Runtime" value={health.data?.status ?? (health.isLoading ? "checking" : "offline")} status={health.data?.status === "pass" ? "pass" : health.isLoading ? "running" : health.data?.status ?? "block"} />
      <HealthCell label="Active job" value={active?.title ?? active?.id.slice(0, 8) ?? "Idle"} status={active ? "running" : "pass"} />
      <HealthCell label="Queue depth" value={`${queue} waiting`} status={queue ? "warning" : "pass"} />
      <HealthCell label="Delivery blockers" value={`${runs.data?.filter((run) => run.delivery_status === "block").length ?? 0} runs`} status={runs.data?.some((run) => run.delivery_status === "block") ? "blocked" : "pass"} />
    </div>
    {pendingJobs.length > 0 && <section className={styles.queuePanel}><header className={styles.queueHead}><div><p>Worker queue</p><h2>Active and waiting jobs</h2></div><StatusPill status={active ? "running" : "queued"} label={`${pendingJobs.length} job${pendingJobs.length === 1 ? "" : "s"}`} /></header><div className={styles.queueList}>{pendingJobs.map((job) => <Link className={styles.queueItem} to={`/runs/${job.id}`} key={job.id}><div><strong>{job.title ?? job.id.slice(0, 8)}</strong><span>{job.current_stage ? titleCase(job.current_stage) : job.status === "queued" ? `Queue position ${job.queue_position ?? "pending"}` : "Worker is starting"}</span></div><StatusPill status={job.status} /></Link>)}</div></section>}
    {runs.error && <ErrorNotice error={runs.error} />}
    {runs.isLoading ? <div className={styles.runGrid}><Skeleton height={230} /><Skeleton height={230} /><Skeleton height={230} /></div> : !runs.data?.length ? <EmptyState title="No runs discovered" detail="Create a new single-video or season run. Existing folders under runs/ will be indexed automatically." action={<Link to="/runs/new"><Button variant="primary">Create first run</Button></Link>} /> : <div className={styles.runGrid}>{runs.data.map((run) => <RunCard key={run.id} run={run} />)}</div>}
  </>;
}

function HealthCell({ label, value, status }: { label: string; value: string; status: string }) {
  return <div className={styles.healthCell}><div><p>{label}</p><strong>{value}</strong></div><StatusPill status={status} /></div>;
}

function RunCard({ run }: { run: RunSummary }) {
  const active = ["running", "starting", "cancel_requested"].includes(run.status);
  return <Link to={`/runs/${run.id}`} className={`${styles.runCard} ${active ? styles.activeCard : ""}`}>
    <div className={styles.runCardHead}><span className={styles.runKind}>{run.kind === "series" ? <Layers3 size={14} /> : <Clapperboard size={14} />}{run.kind}{run.episode_count ? ` / ${run.episode_count} eps` : ""}</span><StatusPill status={run.status} /></div>
    <div className={styles.runCardBody}><h3>{run.title}</h3><p className={styles.runPath}>{run.run_dir}</p><div className={styles.runStage}><span>{run.current_stage ? `Now: ${titleCase(run.current_stage)}` : run.status === "succeeded" ? "Pipeline complete" : "Waiting for next stage"}</span>{active && <div className={styles.runLine} />}</div></div>
    <div className={styles.runCardFoot}><span>Updated {relativeTime(run.updated_at)}</span><span style={{ display: "flex", alignItems: "center", gap: 6 }}>Delivery <StatusPill status={run.delivery_status ?? "unknown"} /><ArrowRight size={13} /></span></div>
  </Link>;
}
