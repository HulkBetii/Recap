import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Clapperboard, FolderOpen, Layers3, Plus, X } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, EmptyState, ErrorNotice, Skeleton, StatusPill } from "../components/Ui";
import { useLocale } from "../i18n";
import type { FsRoot, RunSummary } from "../types";
import { relativeTime, titleCase } from "../utils";
import styles from "./pages.module.css";

export function RunsPage() {
  const { t } = useLocale();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [registerOpen, setRegisterOpen] = useState(false);
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 10_000 });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 5_000 });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.jobs, refetchInterval: 5_000 });
  const active = jobs.data?.find((job) => ["starting", "running", "cancel_requested"].includes(job.status));
  const pendingJobs = jobs.data?.filter((job) => ["queued", "starting", "running", "cancel_requested"].includes(job.status)) ?? [];
  const queue = jobs.data?.filter((job) => job.status === "queued").length ?? health.data?.queue_depth ?? 0;
  const managedRuns = runs.data?.filter((run) => Boolean(run.job_id)) ?? [];
  const importedRuns = runs.data?.filter((run) => !run.job_id) ?? [];

  return <>
    <PageHeader
      eyebrow={t("runs.eyebrow")}
      title={t("runs.title")}
      subtitle={t("runs.subtitle")}
      actions={<div className={styles.actions}><Button onClick={() => setRegisterOpen(true)}><FolderOpen size={15} />{t("runs.register")}</Button><Link className={styles.actionLink} to="/runs/new"><Plus size={16} />{t("runs.new")}</Link></div>}
    />
    <div className={styles.healthStrip}>
      <HealthCell label={t("runs.runtime")} value={health.data?.status ?? (health.isLoading ? "checking" : "offline")} status={health.data?.status === "pass" ? "pass" : health.isLoading ? "running" : health.data?.status ?? "block"} />
      <HealthCell label={t("runs.active")} value={active?.title ?? active?.id.slice(0, 8) ?? t("runs.idle")} status={active ? "running" : "pass"} />
      <HealthCell label={t("runs.queue")} value={`${queue} ${t("runs.waiting")}`} status={queue ? "warning" : "pass"} />
      <HealthCell label={t("runs.blockers")} value={`${runs.data?.filter((run) => run.delivery_status === "block").length ?? 0} ${t("runs.title").toLowerCase()}`} status={runs.data?.some((run) => run.delivery_status === "block") ? "blocked" : "pass"} />
    </div>
    {pendingJobs.length > 0 && <section className={styles.queuePanel}><header className={styles.queueHead}><div><p>{t("runs.workerQueue")}</p><h2>{t("runs.activeWaiting")}</h2></div><StatusPill status={active ? "running" : "queued"} label={t("runs.jobCount", { count: pendingJobs.length })} /></header><div className={styles.queueList}>{pendingJobs.map((job) => <Link className={styles.queueItem} to={`/runs/${job.id}`} key={job.id}><div><strong>{job.title ?? job.id.slice(0, 8)}</strong><span>{job.current_stage ? titleCase(job.current_stage) : job.status === "queued" ? job.queue_position == null ? t("runs.waitingWorker") : t("runs.queuePosition", { position: job.queue_position }) : t("runs.workerStarting")}</span></div><StatusPill status={job.status} /></Link>)}</div></section>}
    {runs.error && <ErrorNotice error={runs.error} />}
    {runs.isLoading ? <div className={styles.runGrid}><Skeleton height={230} /><Skeleton height={230} /><Skeleton height={230} /></div> : !runs.data?.length ? <EmptyState title={t("runs.noRuns")} detail={t("runs.noRunsDetail")} action={<Link className={styles.actionLink} to="/runs/new">{t("runs.createFirst")}</Link>} /> : <>
      {managedRuns.length > 0 && <RunSection title={t("runs.managedTitle")} detail={t("runs.managedDetail")} runs={managedRuns} />}
      {importedRuns.length > 0 && <RunSection title={t("runs.importedTitle")} detail={t("runs.importedDetail")} runs={importedRuns} imported />}
    </>}
    <RegisterRunDialog open={registerOpen} onOpenChange={setRegisterOpen} onRegistered={(run) => { void queryClient.invalidateQueries({ queryKey: ["runs"] }); navigate(`/runs/${run.id}`); }} />
  </>;
}

function HealthCell({ label, value, status }: { label: string; value: string; status: string }) {
  return <div className={styles.healthCell}><div><p>{label}</p><strong>{value}</strong></div><StatusPill status={status} /></div>;
}

function RunSection({ title, detail, runs, imported = false }: { title: string; detail: string; runs: RunSummary[]; imported?: boolean }) {
  const { t } = useLocale();
  return <section className={styles.runSection}><header className={styles.runSectionHead}><div><p>{imported ? t("runs.artifactIndex") : t("runs.workerSupervised")}</p><h2>{title}</h2><span>{detail}</span></div><StatusPill status={imported ? "unknown" : "pass"} label={`${runs.length} run${runs.length === 1 ? "" : "s"}`} /></header><div className={styles.runGrid}>{runs.map((run) => <RunCard key={run.id} run={run} imported={imported} />)}</div></section>;
}

function RunCard({ run, imported = false }: { run: RunSummary; imported?: boolean }) {
  const { t } = useLocale();
  const active = !imported && ["queued", "running", "starting", "cancel_requested"].includes(run.status);
  return <Link to={`/runs/${run.id}`} className={`${styles.runCard} ${active ? styles.activeCard : ""} ${imported ? styles.importedCard : ""}`}>
    <div className={styles.runCardHead}><span className={styles.runKind}>{run.kind === "series" ? <Layers3 size={14} /> : <Clapperboard size={14} />}{run.kind}{run.episode_count ? ` / ${run.episode_count} eps` : ""}</span><StatusPill status={imported ? "unknown" : run.status} label={imported ? t("runs.artifacts") : undefined} /></div>
    <div className={styles.runCardBody}><h3>{run.title}</h3><p className={styles.runPath}>{run.run_dir}</p>{imported ? <div className={styles.runStage}><span>{t("runs.readOnly")}</span><span className={styles.importedHint}>{t("runs.noWorker")}</span></div> : <div className={styles.runStage}><span>{run.current_stage ? `${t("runs.now")}: ${titleCase(run.current_stage)}` : run.status === "succeeded" ? t("detail.pipelineComplete") : run.status === "queued" ? t("runs.queued") : t("runs.nextStage")}</span>{active && <div className={styles.runLine} />}</div>}</div>
    <div className={styles.runCardFoot}><span>{run.updated_at ? t("runs.updated", { time: relativeTime(run.updated_at) }) : t("runs.discovery")}</span><span style={{ display: "flex", alignItems: "center", gap: 6 }}>{t("runs.delivery")} <StatusPill status={run.delivery_status ?? "unknown"} /><ArrowRight size={13} /></span></div>
  </Link>;
}

function RegisterRunDialog({ open, onOpenChange, onRegistered }: { open: boolean; onOpenChange: (open: boolean) => void; onRegistered: (run: RunSummary) => void }) {
  const { t } = useLocale();
  const roots = useQuery({ queryKey: ["fs-roots"], queryFn: api.roots, enabled: open });
  const [root, setRoot] = useState<FsRoot>();
  const [stack, setStack] = useState<Array<{ token: string; name: string }>>([]);
  const currentToken = stack.at(-1)?.token;
  const entries = useQuery({ queryKey: ["fs-entries", root?.id, currentToken], queryFn: () => api.entries(root!.id, currentToken), enabled: open && Boolean(root) });
  const registration = useMutation({ mutationFn: () => api.registerRun(currentToken ?? root!.token), onSuccess: onRegistered });

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setRoot(undefined);
      setStack([]);
      registration.reset();
    }
    onOpenChange(nextOpen);
  };

  return <Dialog.Root open={open} onOpenChange={handleOpenChange}><Dialog.Portal><Dialog.Overlay className={styles.dialogOverlay} /><Dialog.Content className={styles.dialogContent}><div className={styles.dialogHead}><div><Dialog.Title>{t("runs.registerTitle")}</Dialog.Title><Dialog.Description>{t("runs.registerDetail")}</Dialog.Description></div><Dialog.Close asChild><button className={styles.dialogClose} aria-label={t("runs.close")}><X size={17} /></button></Dialog.Close></div><div className={styles.dialogBody}>
    {!root ? <><p className={styles.dialogLabel}>{t("runs.allowedRoots")}</p>{roots.isLoading ? <Skeleton height={120} /> : roots.error ? <ErrorNotice error={roots.error} /> : <div className={styles.rootPicker}>{roots.data?.map((item) => <button className={styles.rootChoice} key={item.id} onClick={() => setRoot(item)}><FolderOpen size={17} /><span><strong>{item.label}</strong><small>{item.path ?? "Path hidden by backend"}</small></span></button>)}</div>}</> : <><div className={styles.dialogBreadcrumb}><button onClick={() => { setRoot(undefined); setStack([]); }}>Roots</button><span>/</span><span>{root.label}</span>{stack.map((item) => <span key={item.token}> / {item.name}</span>)}</div>{entries.isLoading ? <Skeleton height={200} /> : entries.error ? <ErrorNotice error={entries.error} /> : <div className={styles.entryList}>{(entries.data ?? []).filter((entry) => entry.kind === "directory").map((entry) => <button className={styles.entryChoice} key={entry.token} onClick={() => setStack((current) => [...current, { token: entry.token, name: entry.name }])}><FolderOpen size={15} /><span>{entry.name}</span><ArrowRight size={14} /></button>)}{entries.data?.every((entry) => entry.kind !== "directory") && <p className={styles.dialogMuted}>{t("runs.noChildren")}</p>}</div>}<div className={styles.dialogSelected}><span>{t("runs.selectedDirectory")}</span><strong>{[root.label, ...stack.map((item) => item.name)].join(" / ")}</strong></div>{registration.error && <ErrorNotice error={registration.error} />}</>}
  </div><div className={styles.dialogActions}><Dialog.Close asChild><Button variant="ghost">{t("runs.close")}</Button></Dialog.Close><Button variant="primary" disabled={!root || registration.isPending} onClick={() => registration.mutate()}>{registration.isPending ? t("runs.registering") : t("runs.registerFolder")}</Button></div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}
